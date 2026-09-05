#!/usr/bin/env python3
"""Batch-stabilize videos with Gyroflow's command-line interface.

By default no lens profile or external gyro file is supplied, so Gyroflow uses
the motion metadata embedded in each clip plus its normal automatic handling.
Source files are never modified; output goes to a separate tree.

Behaviour notes that are easy to get wrong, all verified against Gyroflow 1.6.3:

* Gyroflow exits 0 even when it fails (missing input, unreadable file). The
  ``[ERROR]`` lines it logs also appear on successful renders. The only
  trustworthy success signal is whether the output file was produced.
* Gyroflow renders to ``<output>.tmp`` and renames on completion, so an
  interrupted run leaves a large partial ``.tmp`` behind.
* ``--preset`` cannot be repeated; a preset file and inline overrides must be
  merged into a single value.
* Unknown preset keys are accepted and silently ignored, so names must match
  the project schema exactly (``adaptive_zoom_window``, not ``adaptive_zoom``).
* Rolling shutter correction is off unless ``frame_readout_time`` is non-zero,
  and only ~11% of bundled lens profiles supply one.
* A corrupt input can make Gyroflow hang indefinitely, hence the timeout.
* Omitting ``bitrate`` lets Gyroflow scale it by resolution (roughly 1 Mbps at
  360p30, 28 Mbps at 1080p60), which is usually too low for delivery.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_GYROFLOW = Path("/Applications/Gyroflow.app/Contents/MacOS/gyroflow")
DEFAULT_FFPROBE = Path(shutil.which("ffprobe") or "ffprobe")
DEFAULT_FFMPEG = Path(shutil.which("ffmpeg") or "ffmpeg")
DEFAULT_EXTENSIONS = (".mp4", ".mov", ".mxf", ".insv")

# Gyroflow has no LUT support of any kind, so a LUT needs a second ffmpeg pass.
# To keep that to a single lossy generation, Gyroflow writes a visually lossless
# ProRes staging file and ffmpeg produces the delivery encode from it.
INTERMEDIATE_SUFFIX = ".gyroflow-intermediate.mov"
# (VideoToolbox encoder, portable fallback) per Gyroflow codec name.
FFMPEG_ENCODERS = {
    "H.264/AVC": ("h264_videotoolbox", "libx264"),
    "H.265/HEVC": ("hevc_videotoolbox", "libx265"),
    "ProRes": ("prores_ks", "prores_ks"),
    "DNxHD": ("dnxhd", "dnxhd"),
}
# ffprobe field -> ffmpeg output flag, so colour tagging survives the LUT pass.
COLOR_TAG_FLAGS = (
    ("color_range", "-color_range"),
    ("color_space", "-colorspace"),
    ("color_primaries", "-color_primaries"),
    ("color_transfer", "-color_trc"),
)

# Timed-metadata markers that indicate usable embedded gyro data.
GYRO_METADATA_MARKERS = ("rtmd", "gpmd", "gopro met", "camm")

# Carries the shooting UTC offset, so its wall clock is the real local time.
QUICKTIME_DATE_TAG = "com.apple.quicktime.creationdate"
# QuickTime's zero epoch is 1904-01-01; cameras with no clock set also emit 1970.
EARLIEST_PLAUSIBLE_YEAR = 1980

SANDBOX_HINT = (
    "This looks like the sandboxed Mac App Store build. Its CLI aborts during "
    "sandbox initialisation and can only read files the user picks in a dialog. "
    "Install the non-sandboxed build instead: brew install --cask gyroflow"
)

_IN_FLIGHT: set[Path] = set()
_IN_FLIGHT_LOCK = threading.Lock()


@dataclass(frozen=True)
class RenderJob:
    source: Path
    destination: Path
    # ProRes staging file when a LUT pass follows; None for a direct render.
    intermediate: Path | None = None

    @property
    def gyroflow_target(self) -> Path:
        return self.intermediate or self.destination


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively batch-stabilize videos with their embedded gyro "
            "metadata. Original files are never modified."
        )
    )
    parser.add_argument("input_dir", type=Path, help="Folder containing source videos")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Destination root (default: INPUT_DIR/gyroflow_stabilized)",
    )
    parser.add_argument(
        "--gyroflow",
        type=Path,
        default=DEFAULT_GYROFLOW,
        help=f"Gyroflow CLI executable (default: {DEFAULT_GYROFLOW})",
    )
    parser.add_argument(
        "--ffprobe",
        type=Path,
        default=DEFAULT_FFPROBE,
        help=f"ffprobe executable used for gyro metadata checks (default: {DEFAULT_FFPROBE})",
    )
    parser.add_argument(
        "--codec",
        default="H.265/HEVC",
        choices=("H.264/AVC", "H.265/HEVC", "ProRes", "DNxHD"),
        help="Output codec (default: H.265/HEVC)",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=100,
        help="Target bitrate in Mbps for H.264/H.265; 0 lets Gyroflow scale it "
        "by resolution (default: 100)",
    )
    parser.add_argument(
        "--max-zoom",
        type=float,
        help="Cap the stabilization crop, as a zoom percentage. "
        "max_zoom = 100 / (1 - crop); e.g. 117.6 allows at most a 15%% crop "
        "per dimension. Omit to use the value set in the Gyroflow GUI.",
    )
    parser.add_argument(
        "--zoom-window",
        type=float,
        help="Adaptive zoom window in seconds; -1 keeps one constant crop for "
        "the whole clip. Omit to use the value set in the Gyroflow GUI.",
    )
    parser.add_argument(
        "--readout-time",
        type=float,
        help="Sensor frame readout time in milliseconds, which enables rolling "
        "shutter (jello) correction. Gyroflow defaults this to 0 (off) and only "
        "about 11%% of its lens profiles supply a value, so it usually has to be "
        "given here. Typical full-frame/APS-C values are 10-20 ms.",
    )
    parser.add_argument(
        "--readout-direction",
        choices=("TopToBottom", "BottomToTop", "LeftToRight", "RightToLeft"),
        help="Sensor readout direction (default TopToBottom). Gyroflow silently "
        "falls back to TopToBottom for unrecognised values.",
    )
    parser.add_argument(
        "--suffix",
        default="_stabilized",
        help="Suffix added to output filenames (default: _stabilized)",
    )
    parser.add_argument(
        "--add-date",
        choices=("prefix", "suffix"),
        help="Put the source clip's recording date in the output filename, "
        "either before the original stem (2026-08-20_clip_stabilized.mp4) or "
        "after it (clip_2026-08-20_stabilized.mp4).",
    )
    parser.add_argument(
        "--date-format",
        default="%Y-%m-%d",
        help="strftime pattern for --add-date (default: %%Y-%%m-%%d). "
        "Use %%Y%%m%%d_%%H%%M%%S to include the time.",
    )
    parser.add_argument(
        "--date-utc",
        action="store_true",
        help="Keep UTC timestamps as UTC instead of converting them to this "
        "machine's local time. Tags that already carry a UTC offset are always "
        "used as written.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Concurrent Gyroflow processes (default: 1; recommended on laptops)",
    )
    parser.add_argument(
        "--preset",
        type=Path,
        help="Optional Gyroflow preset file. Merged with --max-zoom/--zoom-window.",
    )
    parser.add_argument(
        "--extension",
        default=".mp4",
        help="Output container extension (default: .mp4)",
    )
    parser.add_argument(
        "--lut",
        type=Path,
        help="Apply a .cube 3D LUT after stabilization. Gyroflow cannot do this "
        "itself, so the clip is staged as ProRes and ffmpeg produces the final "
        "encode -- one lossy generation, but peak disk use is several times the "
        "delivery size.",
    )
    parser.add_argument(
        "--lut-only",
        action="store_true",
        help="Skip stabilization entirely and only bake the LUT. Gyroflow is "
        "never launched, no ProRes staging file is written, and ffmpeg reads "
        "each source directly. Requires --lut.",
    )
    parser.add_argument(
        "--ffmpeg",
        type=Path,
        default=DEFAULT_FFMPEG,
        help=f"ffmpeg executable used for the LUT pass (default: {DEFAULT_FFMPEG})",
    )
    parser.add_argument(
        "--lut-encoder",
        help="Override the ffmpeg encoder for the LUT pass (default: "
        "VideoToolbox on macOS, libx264/libx265 elsewhere)",
    )
    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep the ProRes staging file instead of deleting it, so the "
        "before/after of the LUT pass can be compared",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Per-file timeout in seconds; 0 disables it. Guards against "
        "Gyroflow hanging forever on a corrupt input (default: 7200)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Process only files directly inside INPUT_DIR",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing outputs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without launching Gyroflow",
    )
    parser.add_argument(
        "--skip-gyro-check",
        action="store_true",
        help="Queue files without probing for embedded gyro metadata",
    )
    parser.add_argument(
        "--require-gyro-metadata",
        action="store_true",
        help="Skip files whose gyro metadata is not recognised. Off by default "
        "because the probe only knows Sony rtmd, GoPro gpmd and CAMM.",
    )
    parser.add_argument(
        "--skip-cli-check",
        action="store_true",
        help="Do not run `gyroflow --version` before starting",
    )
    return parser.parse_args(argv)


def normalized_extension(value: str) -> str:
    return value if value.startswith(".") else f".{value}"


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_videos(
    input_dir: Path,
    output_dir: Path,
    *,
    recursive: bool,
    suffix: str,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
) -> list[Path]:
    allowed = {extension.lower() for extension in extensions}
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    output_dir = output_dir.resolve()
    videos: list[Path] = []

    for candidate in iterator:
        if not candidate.is_file() or candidate.suffix.lower() not in allowed:
            continue
        resolved = candidate.resolve()
        if is_relative_to(resolved, output_dir):
            continue
        if candidate.stem.endswith(suffix):
            continue
        videos.append(candidate)

    return sorted(videos, key=lambda path: str(path).lower())


def parse_media_timestamp(value: object) -> datetime | None:
    """Parse the ISO 8601 spellings ffprobe emits, tolerating `Z` and `+0800`."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    # `+0800` -> `+08:00`, which datetime.fromisoformat needs before 3.11.
    match = re.search(r"([+-]\d{2})(\d{2})$", text)
    if match:
        text = f"{text[: match.start()]}{match.group(1)}:{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year < EARLIEST_PLAUSIBLE_YEAR:
        return None
    return parsed


def probe_recording_date(
    source: Path, ffprobe: Path, *, prefer_utc: bool = False
) -> tuple[datetime | None, str]:
    """Return when the clip was recorded, plus where the value came from.

    Priority: the QuickTime creation date (carries the shooting UTC offset, so
    its wall clock is the real local time), then `creation_time` (normally UTC,
    converted to local time unless prefer_utc), then the file's mtime.
    """
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream_tags",
        "-of",
        "json",
        str(source),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)

    tags: dict[str, object] = {}
    if completed.returncode == 0:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {}
        tags.update(payload.get("format", {}).get("tags") or {})
        for stream in payload.get("streams") or []:
            for key, value in (stream.get("tags") or {}).items():
                tags.setdefault(key, value)

    parsed = parse_media_timestamp(tags.get(QUICKTIME_DATE_TAG))
    if parsed is not None:
        # Already the shooting wall clock; re-basing it would shift the date.
        return parsed.replace(tzinfo=None), QUICKTIME_DATE_TAG

    parsed = parse_media_timestamp(tags.get("creation_time"))
    if parsed is not None:
        if parsed.tzinfo is not None and not prefer_utc:
            parsed = parsed.astimezone()
        return parsed.replace(tzinfo=None), "creation_time"

    try:
        modified = source.stat().st_mtime
    except OSError:
        return None, "unavailable"
    zone = timezone.utc if prefer_utc else None
    return datetime.fromtimestamp(modified, zone).replace(tzinfo=None), "file mtime"


def dated_stem(stem: str, date_text: str | None, position: str | None) -> str:
    """Insert the recording date, keeping the render suffix at the end so the
    output is still recognised as an output."""
    if not date_text or position is None:
        return stem
    if position == "prefix":
        return f"{date_text}_{stem}"
    return f"{stem}_{date_text}"


def make_job(
    source: Path,
    input_dir: Path,
    output_dir: Path,
    *,
    suffix: str,
    output_extension: str,
    date_text: str | None = None,
    date_position: str | None = None,
) -> RenderJob:
    relative = source.relative_to(input_dir)
    stem = dated_stem(source.stem, date_text, date_position)
    destination = output_dir / relative.parent / f"{stem}{suffix}{output_extension}"
    return RenderJob(source=source, destination=destination)


def build_preset_value(
    preset_file: Path | None,
    *,
    max_zoom: float | None,
    zoom_window: float | None,
    readout_time: float | None = None,
    readout_direction: str | None = None,
) -> str | None:
    """Return the single value for --preset, or None when no preset is wanted.

    Gyroflow rejects a repeated --preset ("duplicate values provided"), so a
    preset file plus inline overrides have to be merged here.

    Key names must match the project schema exactly; Gyroflow silently ignores
    unknown keys. `adaptive_zoom_window` is the real name -- `adaptive_zoom`
    parses without complaint and does nothing.
    """
    overrides: dict[str, object] = {}
    if max_zoom is not None:
        overrides["max_zoom"] = max_zoom
    if zoom_window is not None:
        overrides["adaptive_zoom_window"] = zoom_window
    if readout_time is not None:
        overrides["frame_readout_time"] = readout_time
    if readout_direction is not None:
        overrides["frame_readout_direction"] = readout_direction

    if not overrides:
        return str(preset_file) if preset_file is not None else None

    data: dict = {}
    if preset_file is not None:
        data = json.loads(preset_file.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"preset file is not a JSON object: {preset_file}")

    data.setdefault("version", 2)
    stabilization: dict[str, object] = dict(data.get("stabilization") or {})
    stabilization.update(overrides)
    data["stabilization"] = stabilization
    return json.dumps(data, separators=(",", ":"))


def build_command(
    job: RenderJob,
    *,
    gyroflow: Path,
    codec: str,
    bitrate: int,
    overwrite: bool,
    preset_value: str | None,
) -> list[str]:
    output_parameters: dict[str, object] = {
        "use_gpu": True,
        "audio": True,
        "output_path": str(job.gyroflow_target),
    }
    if job.intermediate is not None:
        # Stage losslessly; the delivery codec is applied by the LUT pass.
        output_parameters["codec"] = "ProRes"
    else:
        if codec:
            output_parameters["codec"] = codec
        if bitrate:
            output_parameters["bitrate"] = bitrate

    command = [
        str(gyroflow),
        str(job.source),
        "--out-params",
        json.dumps(output_parameters, separators=(",", ":")),
        "--stdout-progress",
    ]
    if overwrite:
        command.append("--overwrite")
    if preset_value is not None:
        command.extend(("--preset", preset_value))
    return command


def pick_lut_encoder(codec: str, override: str | None = None) -> str:
    if override:
        return override
    accelerated, portable = FFMPEG_ENCODERS.get(codec, ("libx264", "libx264"))
    return accelerated if platform.system() == "Darwin" else portable


def probe_color_tags(source: Path, ffprobe: Path) -> dict[str, str]:
    """Read the colour tagging so the LUT pass can reproduce it verbatim.

    Getting this wrong is the usual cause of a LUT looking off: lut3d operates
    on the RGB that results from the tagged range, so a limited/full mismatch
    feeds the LUT the wrong signal.
    """
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=" + ",".join(field for field, _ in COLOR_TAG_FLAGS),
        "-of",
        "json",
        str(source),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    streams = payload.get("streams") or [{}]
    stream = streams[0] if streams else {}
    tags: dict[str, str] = {}
    for field, _ in COLOR_TAG_FLAGS:
        value = stream.get(field)
        if value and str(value).lower() not in ("unknown", "unspecified", "reserved"):
            tags[field] = str(value)
    return tags


def build_lut_command(
    job: RenderJob,
    *,
    ffmpeg: Path,
    lut: Path,
    encoder: str,
    bitrate: int,
    color_tags: dict[str, str],
) -> list[str]:
    """ffmpeg pass that bakes the LUT and produces the delivery encode.

    The 16-bit intermediate format keeps the LUT from banding gradients; the
    output pixel format is set back to 8-bit 4:2:0 for delivery.
    """
    # With stabilization the ProRes staging file is the input; in LUT-only mode
    # the source is already the master and ffmpeg reads it directly.
    lut_input = job.intermediate if job.intermediate is not None else job.source
    chain = f"format=yuv444p16le,lut3d=file={_escape_filter_path(lut)},format=yuv420p"
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(lut_input),
        "-vf",
        chain,
        "-c:v",
        encoder,
    ]
    if bitrate and encoder not in ("prores_ks",):
        command.extend(("-b:v", f"{bitrate}M"))
    if encoder.startswith("hevc"):
        command.extend(("-tag:v", "hvc1"))
    for field, flag in COLOR_TAG_FLAGS:
        if field in color_tags:
            command.extend((flag, color_tags[field]))
    command.extend(("-c:a", "copy", str(job.destination)))
    return command


def _escape_filter_path(path: Path) -> str:
    """ffmpeg filter arguments treat : \\ and ' specially."""
    text = str(path)
    for char in ("\\", ":", "'", "[", "]", ","):
        text = text.replace(char, f"\\{char}")
    return text


def has_gyro_metadata(source: Path, ffprobe: Path) -> tuple[bool, str]:
    """Return whether ffprobe exposes a known gyro/timed-metadata track.

    Recognises Sony `rtmd`, GoPro `gpmd`/"GoPro MET" and CAMM. Other cameras
    may still carry usable data, so a negative result is a warning by default.
    """
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,codec_long_name,codec_tag_string:stream_tags=handler_name",
        "-of",
        "json",
        str(source),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"ffprobe exited with {completed.returncode}"
        return False, f"metadata probe failed: {detail}"

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return False, f"metadata probe returned invalid JSON: {error}"

    for stream in payload.get("streams", []):
        values = [
            stream.get("codec_name", ""),
            stream.get("codec_long_name", ""),
            stream.get("codec_tag_string", ""),
            stream.get("tags", {}).get("handler_name", ""),
        ]
        normalized = " ".join(str(value).lower() for value in values)
        for marker in GYRO_METADATA_MARKERS:
            if marker in normalized:
                return True, f"{marker} timed-metadata track detected"

    return False, "no known gyro metadata track found"


def probe_gyroflow_cli(executable: Path, *, timeout: int = 120) -> tuple[bool, str]:
    """Run `gyroflow --version` so an unusable binary fails fast and clearly.

    The sandboxed Mac App Store build aborts in `_libsecinit_appsandbox` before
    main() runs, which surfaces here as SIGTRAP (returncode -5).
    """
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"`{executable} --version` timed out after {timeout}s"
    except OSError as error:
        return False, f"could not run {executable}: {error}"

    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 and "Gyroflow" in output:
        for line in output.splitlines():
            if line.startswith("Gyroflow v"):
                return True, line.strip()
        return True, "Gyroflow CLI responded"

    message = f"`{executable} --version` failed (exit {completed.returncode})"
    looks_sandboxed = completed.returncode == -5 or (
        platform.system() == "Darwin" and (executable.parents[1] / "_MASReceipt").is_dir()
    )
    if looks_sandboxed:
        message = f"{message}. {SANDBOX_HINT}"
    return False, message


def _discard_partial(destination: Path) -> None:
    """Remove the `<output>.tmp` Gyroflow leaves behind on an aborted render."""
    partial = destination.with_name(destination.name + ".tmp")
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"Could not remove partial file {partial}: {error}", file=sys.stderr)


def _produced(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _drop(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"Could not remove {path}: {error}", file=sys.stderr)


@dataclass(frozen=True)
class Stage:
    """One external command in a job.

    ``trust_exit_code`` is False for Gyroflow, which returns 0 even when it
    fails; there the produced file is the only reliable signal. ffmpeg does
    report failure and can leave a truncated file, so its exit code decides.
    """

    label: str
    tool: str
    command: list[str]
    output: Path
    trust_exit_code: bool


def render_job(
    stages: Sequence[Stage],
    destination: Path,
    *,
    dry_run: bool,
    timeout: int = 0,
    keep_intermediate: bool = False,
) -> tuple[bool, str]:
    if dry_run:
        joined = "  &&  ".join(shlex.join(stage.command) for stage in stages)
        return True, f"DRY RUN: {joined}"

    destination.parent.mkdir(parents=True, exist_ok=True)
    outputs = [stage.output for stage in stages]
    intermediates = [path for path in outputs if path != destination]

    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.update(outputs)
    try:
        for index, stage in enumerate(stages):
            print(f"\n{stage.label}: {destination.name}", flush=True)
            try:
                completed = subprocess.run(
                    stage.command, check=False, timeout=timeout or None
                )
            except subprocess.TimeoutExpired:
                _discard_partial(stage.output)
                _drop(stage.output)
                return False, (
                    f"FAILED ({stage.tool} timed out after {timeout}s): {destination}"
                )
            except KeyboardInterrupt:
                _discard_partial(stage.output)
                raise

            _discard_partial(stage.output)
            failed = not _produced(stage.output) or (
                stage.trust_exit_code and completed.returncode != 0
            )
            if failed:
                _drop(stage.output)
                detail = f"{stage.tool} exited {completed.returncode}"
                survivors = [
                    str(path) for path in outputs[:index] if path.is_file()
                ]
                kept = f"; kept {', '.join(survivors)}" if survivors else ""
                return False, (
                    f"FAILED (no output from {stage.tool}; {detail}{kept}): "
                    f"{destination}"
                )
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.difference_update(outputs)

    if not keep_intermediate:
        for path in intermediates:
            _drop(path)
    return True, f"OK: {destination}"


def _raise_on_sigterm(_signum: int, _frame: object) -> None:
    """Route SIGTERM through the Ctrl-C path so partial .tmp files still get
    cleaned up when a wrapper (the GUI's Stop button, `kill`) ends the run."""
    raise KeyboardInterrupt


def _cleanup_in_flight() -> None:
    with _IN_FLIGHT_LOCK:
        pending = list(_IN_FLIGHT)
        _IN_FLIGHT.clear()
    for destination in pending:
        _discard_partial(destination)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (args.output_dir or input_dir / "gyroflow_stabilized").expanduser().resolve()
    gyroflow = args.gyroflow.expanduser().resolve()
    ffprobe = args.ffprobe.expanduser().resolve()
    output_extension = normalized_extension(args.extension)
    # Gyro metadata is irrelevant when no stabilization happens.
    check_gyro = not args.skip_gyro_check and not args.lut_only

    if not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not args.lut_only and (
        not gyroflow.is_file() or not os.access(gyroflow, os.X_OK)
    ):
        print(f"Gyroflow executable is unavailable: {gyroflow}", file=sys.stderr)
        return 2
    if (check_gyro or args.add_date) and (
        not ffprobe.is_file() or not os.access(ffprobe, os.X_OK)
    ):
        print(f"ffprobe executable is unavailable: {ffprobe}", file=sys.stderr)
        return 2
    if args.add_date:
        sample = datetime(2026, 8, 20, 14, 23, 11).strftime(args.date_format)
        if not sample or os.sep in sample or (os.altsep and os.altsep in sample):
            print(
                f"--date-format must render a non-empty name without path "
                f"separators (got {sample!r})",
                file=sys.stderr,
            )
            return 2
    if args.jobs < 1:
        print("--jobs must be at least 1", file=sys.stderr)
        return 2
    if args.bitrate < 0:
        print("--bitrate must be zero or positive", file=sys.stderr)
        return 2
    if args.timeout < 0:
        print("--timeout must be zero or positive", file=sys.stderr)
        return 2
    if args.max_zoom is not None and args.max_zoom < 100:
        print("--max-zoom must be at least 100 (100 = no crop)", file=sys.stderr)
        return 2
    if args.readout_time is not None and args.readout_time < 0:
        print("--readout-time must be zero or positive (0 = correction off)", file=sys.stderr)
        return 2
    if args.preset is not None and not args.preset.expanduser().is_file():
        print(f"Preset does not exist: {args.preset}", file=sys.stderr)
        return 2

    if args.lut_only and args.lut is None:
        print("--lut-only requires --lut", file=sys.stderr)
        return 2

    lut: Path | None = None
    ffmpeg = args.ffmpeg.expanduser()
    lut_encoder = ""
    if args.lut is not None:
        lut = args.lut.expanduser().resolve()
        if not lut.is_file():
            print(f"LUT does not exist: {lut}", file=sys.stderr)
            return 2
        if lut.suffix.lower() != ".cube":
            print(f"LUT must be a .cube file: {lut}", file=sys.stderr)
            return 2
        resolved_ffmpeg = shutil.which(str(ffmpeg)) or (
            str(ffmpeg) if ffmpeg.is_file() and os.access(ffmpeg, os.X_OK) else None
        )
        if resolved_ffmpeg is None:
            print(f"ffmpeg executable is unavailable: {ffmpeg}", file=sys.stderr)
            return 2
        ffmpeg = Path(resolved_ffmpeg)
        lut_encoder = pick_lut_encoder(args.codec, args.lut_encoder)

    if not args.dry_run and not args.skip_cli_check and not args.lut_only:
        usable, detail = probe_gyroflow_cli(gyroflow)
        if not usable:
            print(detail, file=sys.stderr)
            return 2
        print(detail)

    preset_file = args.preset.expanduser().resolve() if args.preset is not None else None
    try:
        preset_value = build_preset_value(
            preset_file,
            max_zoom=args.max_zoom,
            zoom_window=args.zoom_window,
            readout_time=args.readout_time,
            readout_direction=args.readout_direction,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Could not build preset: {error}", file=sys.stderr)
        return 2

    videos = discover_videos(
        input_dir,
        output_dir,
        recursive=not args.no_recursive,
        suffix=args.suffix,
    )
    if not videos:
        print(f"No source videos found in {input_dir}")
        return 0

    eligible_videos: list[Path] = []
    missing_gyro = 0
    if not check_gyro:
        eligible_videos = videos
    else:
        for source in videos:
            has_gyro, reason = has_gyro_metadata(source, ffprobe)
            if has_gyro:
                print(f"GYRO OK: {source}")
                eligible_videos.append(source)
            elif args.require_gyro_metadata:
                print(f"SKIP (no recognised gyro metadata): {source} [{reason}]")
                missing_gyro += 1
            else:
                print(f"WARNING (no recognised gyro metadata, rendering anyway): {source} [{reason}]")
                eligible_videos.append(source)

    jobs: list[RenderJob] = []
    for source in eligible_videos:
        date_text: str | None = None
        if args.add_date:
            recorded, origin = probe_recording_date(
                source, ffprobe, prefer_utc=args.date_utc
            )
            if recorded is None:
                print(f"WARNING (no recording date, name left undated): {source}")
            else:
                date_text = recorded.strftime(args.date_format)
                if origin == "file mtime":
                    print(
                        f"NOTE (no date metadata, using file mtime {date_text}): {source}"
                    )
        job = make_job(
            source,
            input_dir,
            output_dir,
            suffix=args.suffix,
            output_extension=output_extension,
            date_text=date_text,
            date_position=args.add_date,
        )
        if lut is not None and not args.lut_only:
            job = RenderJob(
                source=job.source,
                destination=job.destination,
                intermediate=job.destination.with_name(
                    job.destination.stem + INTERMEDIATE_SUFFIX
                ),
            )
        jobs.append(job)

    planned: list[tuple[list[Stage], RenderJob]] = []
    skipped = 0
    for job in jobs:
        if job.destination.exists() and not args.overwrite:
            print(f"SKIP (already exists): {job.destination}")
            skipped += 1
            continue

        stages: list[Stage] = []
        if not args.lut_only:
            stages.append(
                Stage(
                    label="Rendering",
                    tool="Gyroflow",
                    command=build_command(
                        job,
                        gyroflow=gyroflow,
                        codec=args.codec,
                        bitrate=args.bitrate,
                        overwrite=args.overwrite,
                        preset_value=preset_value,
                    ),
                    output=job.gyroflow_target,
                    trust_exit_code=False,
                )
            )
        if lut is not None:
            stages.append(
                Stage(
                    label="Applying LUT",
                    tool="ffmpeg",
                    command=build_lut_command(
                        job,
                        ffmpeg=ffmpeg,
                        lut=lut,
                        encoder=lut_encoder,
                        bitrate=args.bitrate,
                        color_tags=(
                            {} if args.dry_run else probe_color_tags(job.source, ffprobe)
                        ),
                    ),
                    output=job.destination,
                    trust_exit_code=True,
                )
            )
        planned.append((stages, job))

    mode = (
        "LUT only"
        if args.lut_only
        else ("stabilize + LUT" if lut is not None else "stabilize")
    )
    print(
        f"Mode: {mode}; found {len(videos)} video(s); gyro metadata missing "
        f"{missing_gyro}; queued {len(planned)}; existing outputs skipped "
        f"{skipped}; output: {output_dir}"
    )
    if not planned:
        return 1 if missing_gyro else 0

    results: list[tuple[bool, str]] = []
    previous_sigterm = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = [
                executor.submit(
                    render_job,
                    stages,
                    job.destination,
                    dry_run=args.dry_run,
                    timeout=args.timeout,
                    keep_intermediate=args.keep_intermediate,
                )
                for stages, job in planned
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                print(result[1], flush=True)
    except KeyboardInterrupt:
        _cleanup_in_flight()
        print("\nInterrupted; partial .tmp files removed.", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    failed = sum(not success for success, _ in results)
    print(
        f"Completed: {len(results) - failed}; failed: {failed}; "
        f"missing gyro: {missing_gyro}; existing outputs skipped: {skipped}"
    )
    return 1 if failed or missing_gyro else 0


if __name__ == "__main__":
    raise SystemExit(main())
