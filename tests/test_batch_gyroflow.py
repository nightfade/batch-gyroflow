from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "batch_gyroflow.py"
SPEC = importlib.util.spec_from_file_location("batch_gyroflow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DiscoveryTests(unittest.TestCase):
    def test_discover_videos_is_recursive_and_excludes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "gyroflow_stabilized"
            (root / "nested").mkdir()
            output.mkdir()
            (root / "A.MP4").touch()
            (root / "nested" / "B.mov").touch()
            (root / "already_stabilized.mp4").touch()
            (output / "old.mp4").touch()
            (root / "notes.txt").touch()

            videos = MODULE.discover_videos(
                root, output, recursive=True, suffix="_stabilized"
            )

            self.assertEqual(videos, [root / "A.MP4", root / "nested" / "B.mov"])

    def test_make_job_preserves_relative_tree(self) -> None:
        job = MODULE.make_job(
            Path("/input/day1/clip.MP4"),
            Path("/input"),
            Path("/output"),
            suffix="_stabilized",
            output_extension=".mp4",
        )
        self.assertEqual(job.destination, Path("/output/day1/clip_stabilized.mp4"))


class RecordingDateTests(unittest.TestCase):
    def _probe(self, tags: dict, **kwargs: object) -> tuple[object, str]:
        payload = {"format": {"tags": tags}, "streams": []}
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr=""
            )
            return MODULE.probe_recording_date(
                Path("clip.MP4"), Path("ffprobe"), **kwargs
            )

    def test_quicktime_tag_wall_clock_is_used_as_written(self) -> None:
        """It already carries the shooting offset; re-basing it shifts the date."""
        recorded, origin = self._probe(
            {"com.apple.quicktime.creationdate": "2026-08-20T00:23:11+0800"}
        )
        self.assertEqual(recorded.strftime("%Y-%m-%d %H:%M"), "2026-08-20 00:23")
        self.assertEqual(origin, MODULE.QUICKTIME_DATE_TAG)

    def test_quicktime_tag_wins_over_creation_time(self) -> None:
        _, origin = self._probe(
            {
                "com.apple.quicktime.creationdate": "2026-08-20T14:23:11+0800",
                "creation_time": "2019-01-01T00:00:00.000000Z",
            }
        )
        self.assertEqual(origin, MODULE.QUICKTIME_DATE_TAG)

    def test_utc_creation_time_is_kept_when_requested(self) -> None:
        recorded, origin = self._probe(
            {"creation_time": "2026-08-20T06:23:11.000000Z"}, prefer_utc=True
        )
        self.assertEqual(recorded.strftime("%Y-%m-%d %H:%M"), "2026-08-20 06:23")
        self.assertEqual(origin, "creation_time")

    def test_falls_back_to_file_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clip = Path(temporary) / "clip.MP4"
            clip.touch()
            payload = {"format": {"tags": {}}, "streams": []}
            with mock.patch.object(MODULE.subprocess, "run") as run:
                run.return_value = SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload), stderr=""
                )
                recorded, origin = MODULE.probe_recording_date(clip, Path("ffprobe"))
        self.assertIsNotNone(recorded)
        self.assertEqual(origin, "file mtime")

    def test_quicktime_zero_epoch_is_rejected(self) -> None:
        self.assertIsNone(MODULE.parse_media_timestamp("1904-01-01T00:00:00Z"))
        self.assertIsNone(MODULE.parse_media_timestamp("1970-01-01T00:00:00Z"))

    def test_unparseable_values_are_rejected(self) -> None:
        for value in ("", None, "not a date", "0000-00-00"):
            self.assertIsNone(MODULE.parse_media_timestamp(value))


class DatedNameTests(unittest.TestCase):
    def _destination(self, **kwargs: object) -> Path:
        return MODULE.make_job(
            Path("/input/day1/clip.MP4"),
            Path("/input"),
            Path("/output"),
            suffix="_stabilized",
            output_extension=".mp4",
            **kwargs,
        ).destination

    def test_date_prefix(self) -> None:
        self.assertEqual(
            self._destination(date_text="2026-08-20", date_position="prefix"),
            Path("/output/day1/2026-08-20_clip_stabilized.mp4"),
        )

    def test_date_suffix_keeps_render_suffix_last(self) -> None:
        """Outputs must still end with --suffix so discover_videos skips them."""
        destination = self._destination(date_text="2026-08-20", date_position="suffix")
        self.assertEqual(
            destination, Path("/output/day1/clip_2026-08-20_stabilized.mp4")
        )
        self.assertTrue(destination.stem.endswith("_stabilized"))

    def test_missing_date_leaves_the_name_unchanged(self) -> None:
        self.assertEqual(
            self._destination(date_text=None, date_position="prefix"),
            Path("/output/day1/clip_stabilized.mp4"),
        )


class CommandTests(unittest.TestCase):
    def _command(self, **kwargs: object) -> list[str]:
        job = MODULE.RenderJob(Path("/input/clip.MP4"), Path("/output/clip_stabilized.mp4"))
        defaults: dict = dict(
            gyroflow=Path("/Applications/Gyroflow.app/Contents/MacOS/gyroflow"),
            codec="H.265/HEVC",
            bitrate=100,
            overwrite=False,
            preset_value=None,
        )
        defaults.update(kwargs)
        return MODULE.build_command(job, **defaults)

    def test_command_uses_embedded_metadata_without_lens_or_gyro_arguments(self) -> None:
        command = self._command()
        params = json.loads(command[command.index("--out-params") + 1])

        self.assertEqual(params["output_path"], "/output/clip_stabilized.mp4")
        self.assertEqual(params["codec"], "H.265/HEVC")
        self.assertEqual(params["bitrate"], 100)
        self.assertNotIn("--gyro-file", command)
        self.assertNotIn("--preset", command)

    def test_zero_bitrate_is_omitted_so_gyroflow_scales_it(self) -> None:
        command = self._command(bitrate=0)
        params = json.loads(command[command.index("--out-params") + 1])
        self.assertNotIn("bitrate", params)

    def test_preset_value_is_passed_once(self) -> None:
        command = self._command(preset_value='{"version":2}')
        self.assertEqual(command.count("--preset"), 1)


class LutStageTests(unittest.TestCase):
    def _job(self) -> object:
        return MODULE.RenderJob(
            source=Path("/input/clip.MP4"),
            destination=Path("/output/clip_stabilized.mp4"),
            intermediate=Path("/output/clip_stabilized.gyroflow-intermediate.mov"),
        )

    def test_gyroflow_stages_prores_when_a_lut_follows(self) -> None:
        """The delivery codec is applied by ffmpeg, so stage losslessly."""
        command = MODULE.build_command(
            self._job(),
            gyroflow=Path("/bin/gyroflow"),
            codec="H.265/HEVC",
            bitrate=100,
            overwrite=False,
            preset_value=None,
        )
        params = json.loads(command[command.index("--out-params") + 1])
        self.assertEqual(params["codec"], "ProRes")
        self.assertNotIn("bitrate", params)
        self.assertTrue(params["output_path"].endswith(".gyroflow-intermediate.mov"))

    def test_direct_render_still_uses_the_delivery_codec(self) -> None:
        job = MODULE.RenderJob(
            Path("/input/clip.MP4"), Path("/output/clip_stabilized.mp4")
        )
        command = MODULE.build_command(
            job,
            gyroflow=Path("/bin/gyroflow"),
            codec="H.265/HEVC",
            bitrate=100,
            overwrite=False,
            preset_value=None,
        )
        params = json.loads(command[command.index("--out-params") + 1])
        self.assertEqual(params["codec"], "H.265/HEVC")
        self.assertEqual(params["bitrate"], 100)

    def test_lut_command_reads_the_intermediate_and_writes_the_destination(self) -> None:
        job = self._job()
        command = MODULE.build_lut_command(
            job,
            ffmpeg=Path("/bin/ffmpeg"),
            lut=Path("/luts/grade.cube"),
            encoder="hevc_videotoolbox",
            bitrate=100,
            color_tags={"color_range": "tv", "color_space": "bt709"},
        )
        self.assertEqual(command[command.index("-i") + 1], str(job.intermediate))
        self.assertEqual(command[-1], str(job.destination))
        self.assertEqual(command[command.index("-c:a") + 1], "copy")

    def test_lut_chain_uses_a_high_precision_intermediate_format(self) -> None:
        chain = MODULE.build_lut_command(
            self._job(),
            ffmpeg=Path("/bin/ffmpeg"),
            lut=Path("/luts/grade.cube"),
            encoder="libx265",
            bitrate=0,
            color_tags={},
        )
        filters = chain[chain.index("-vf") + 1]
        self.assertTrue(filters.startswith("format=yuv444p16le,lut3d="))
        self.assertTrue(filters.endswith(",format=yuv420p"))

    def test_color_tags_are_forwarded_so_the_lut_sees_the_right_signal(self) -> None:
        command = MODULE.build_lut_command(
            self._job(),
            ffmpeg=Path("/bin/ffmpeg"),
            lut=Path("/luts/grade.cube"),
            encoder="libx265",
            bitrate=50,
            color_tags={"color_range": "tv", "color_transfer": "bt709"},
        )
        self.assertEqual(command[command.index("-color_range") + 1], "tv")
        self.assertEqual(command[command.index("-color_trc") + 1], "bt709")
        self.assertNotIn("-colorspace", command)

    def test_filter_path_special_characters_are_escaped(self) -> None:
        command = MODULE.build_lut_command(
            self._job(),
            ffmpeg=Path("/bin/ffmpeg"),
            lut=Path("/luts/my grade:v2.cube"),
            encoder="libx265",
            bitrate=0,
            color_tags={},
        )
        self.assertIn("my grade\\:v2.cube", command[command.index("-vf") + 1])

    def test_encoder_choice_prefers_videotoolbox_on_macos(self) -> None:
        with mock.patch.object(MODULE.platform, "system", return_value="Darwin"):
            self.assertEqual(MODULE.pick_lut_encoder("H.265/HEVC"), "hevc_videotoolbox")
        with mock.patch.object(MODULE.platform, "system", return_value="Linux"):
            self.assertEqual(MODULE.pick_lut_encoder("H.265/HEVC"), "libx265")

    def test_encoder_override_wins(self) -> None:
        self.assertEqual(MODULE.pick_lut_encoder("H.265/HEVC", "libsvtav1"), "libsvtav1")

    def test_unknown_color_values_are_dropped(self) -> None:
        payload = {
            "streams": [
                {"color_range": "tv", "color_space": "unknown", "color_primaries": ""}
            ]
        }
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr=""
            )
            tags = MODULE.probe_color_tags(Path("clip.mov"), Path("ffprobe"))
        self.assertEqual(tags, {"color_range": "tv"})


def gyroflow_stage(output: Path) -> object:
    """Gyroflow exits 0 even on failure, so its exit code is not trusted."""
    return MODULE.Stage(
        label="Rendering",
        tool="Gyroflow",
        command=["gyroflow"],
        output=output,
        trust_exit_code=False,
    )


def lut_stage(output: Path) -> object:
    return MODULE.Stage(
        label="Applying LUT",
        tool="ffmpeg",
        command=["ffmpeg"],
        output=output,
        trust_exit_code=True,
    )


class LutRenderTests(unittest.TestCase):
    def _run_two_stage(self, temporary: str, *, lut_ok: bool, keep: bool = False):
        destination = Path(temporary) / "clip_stabilized.mp4"
        intermediate = Path(temporary) / "clip_stabilized.gyroflow-intermediate.mov"

        def fake_run(command, **_kwargs):
            if command[0] == "gyroflow":
                intermediate.write_bytes(b"prores")
                return SimpleNamespace(returncode=0)
            if lut_ok:
                destination.write_bytes(b"graded")
                return SimpleNamespace(returncode=0)
            destination.write_bytes(b"truncated")
            return SimpleNamespace(returncode=1)

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            result = MODULE.render_job(
                [gyroflow_stage(intermediate), lut_stage(destination)],
                destination,
                dry_run=False,
                keep_intermediate=keep,
            )
        return result, destination, intermediate

    def test_intermediate_is_deleted_after_a_successful_lut_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (success, message), _, intermediate = self._run_two_stage(
                temporary, lut_ok=True
            )
            self.assertTrue(success)
            self.assertTrue(message.startswith("OK:"))
            self.assertFalse(intermediate.exists())

    def test_keep_intermediate_leaves_the_prores_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (success, _), _, intermediate = self._run_two_stage(
                temporary, lut_ok=True, keep=True
            )
            self.assertTrue(success)
            self.assertTrue(intermediate.exists())

    def test_failed_lut_pass_keeps_the_render_and_drops_the_bad_output(self) -> None:
        """The Gyroflow render is the expensive half; do not throw it away."""
        with tempfile.TemporaryDirectory() as temporary:
            (success, message), destination, intermediate = self._run_two_stage(
                temporary, lut_ok=False
            )
            self.assertFalse(success)
            self.assertIn("no output from ffmpeg", message)
            self.assertIn(str(intermediate), message)
            self.assertFalse(destination.exists())
            self.assertTrue(intermediate.exists())

    def test_dry_run_shows_both_stages(self) -> None:
        success, message = MODULE.render_job(
            [
                gyroflow_stage(Path("/output/mid.mov")),
                lut_stage(Path("/output/clip_stabilized.mp4")),
            ],
            Path("/output/clip_stabilized.mp4"),
            dry_run=True,
        )
        self.assertTrue(success)
        self.assertIn("gyroflow", message)
        self.assertIn("ffmpeg", message)


class LutOnlyTests(unittest.TestCase):
    """LUT-only mode: no Gyroflow, no staging file, ffmpeg reads the source."""

    def test_lut_only_job_has_no_intermediate_and_reads_the_source(self) -> None:
        job = MODULE.RenderJob(
            source=Path("/input/clip.MP4"),
            destination=Path("/output/clip_graded.mp4"),
        )
        command = MODULE.build_lut_command(
            job,
            ffmpeg=Path("/bin/ffmpeg"),
            lut=Path("/luts/grade.cube"),
            encoder="libx265",
            bitrate=50,
            color_tags={},
        )
        self.assertEqual(command[command.index("-i") + 1], str(job.source))
        self.assertEqual(command[-1], str(job.destination))

    def test_single_stage_leaves_no_intermediate_to_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_graded.mp4"

            def fake_run(_command, **_kwargs):
                destination.write_bytes(b"graded")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
                success, message = MODULE.render_job(
                    [lut_stage(destination)], destination, dry_run=False
                )
            self.assertTrue(success)
            self.assertTrue(destination.exists())
            self.assertTrue(message.startswith("OK:"))

    def test_ffmpeg_nonzero_exit_fails_even_with_a_non_empty_file(self) -> None:
        """ffmpeg can leave a truncated file, so its exit code is trusted."""
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_graded.mp4"

            def fake_run(_command, **_kwargs):
                destination.write_bytes(b"truncated")
                return SimpleNamespace(returncode=1)

            with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
                success, _ = MODULE.render_job(
                    [lut_stage(destination)], destination, dry_run=False
                )
            self.assertFalse(success)
            self.assertFalse(destination.exists())


class PresetMergeTests(unittest.TestCase):
    """Gyroflow rejects a repeated --preset, so merging happens in-script."""

    def test_no_preset_and_no_overrides_yields_none(self) -> None:
        self.assertIsNone(
            MODULE.build_preset_value(None, max_zoom=None, zoom_window=None)
        )

    def test_preset_file_alone_is_passed_by_path(self) -> None:
        value = MODULE.build_preset_value(
            Path("/presets/mine.gyroflow"), max_zoom=None, zoom_window=None
        )
        self.assertEqual(value, "/presets/mine.gyroflow")

    def test_overrides_alone_build_inline_json(self) -> None:
        value = MODULE.build_preset_value(None, max_zoom=117.6, zoom_window=-1)
        payload = json.loads(value)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["stabilization"]["max_zoom"], 117.6)

    def test_zoom_window_uses_the_schema_key_gyroflow_actually_reads(self) -> None:
        """`adaptive_zoom` parses fine but is ignored; the real key is
        `adaptive_zoom_window`. Verified by exporting a Gyroflow project."""
        payload = json.loads(
            MODULE.build_preset_value(None, max_zoom=None, zoom_window=-1)
        )
        self.assertEqual(payload["stabilization"]["adaptive_zoom_window"], -1)
        self.assertNotIn("adaptive_zoom", payload["stabilization"])

    def test_rolling_shutter_overrides_are_emitted(self) -> None:
        payload = json.loads(
            MODULE.build_preset_value(
                None,
                max_zoom=None,
                zoom_window=None,
                readout_time=15.6,
                readout_direction="BottomToTop",
            )
        )
        self.assertEqual(payload["stabilization"]["frame_readout_time"], 15.6)
        self.assertEqual(payload["stabilization"]["frame_readout_direction"], "BottomToTop")

    def test_readout_time_alone_is_enough_to_build_a_preset(self) -> None:
        self.assertIsNotNone(
            MODULE.build_preset_value(
                None, max_zoom=None, zoom_window=None, readout_time=12.0
            )
        )

    def test_overrides_merge_into_preset_file_without_dropping_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preset = Path(temporary) / "mine.gyroflow"
            preset.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "stabilization": {"fov": 1.2, "max_zoom": 200},
                        "output": {"codec": "ProRes"},
                    }
                )
            )

            payload = json.loads(
                MODULE.build_preset_value(preset, max_zoom=117.6, zoom_window=None)
            )

            self.assertEqual(payload["stabilization"]["max_zoom"], 117.6)
            self.assertEqual(payload["stabilization"]["fov"], 1.2)
            self.assertEqual(payload["output"]["codec"], "ProRes")


class GyroMetadataTests(unittest.TestCase):
    def _probe(self, streams: list[dict]) -> tuple[bool, str]:
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout=json.dumps({"streams": streams}), stderr=""
            )
            return MODULE.has_gyro_metadata(Path("clip.MP4"), Path("ffprobe"))

    def test_detects_sony_rtmd_track(self) -> None:
        present, reason = self._probe(
            [{"codec_type": "data", "codec_tag_string": "rtmd"}]
        )
        self.assertTrue(present)
        self.assertIn("rtmd", reason)

    def test_detects_gopro_gpmd_track(self) -> None:
        present, reason = self._probe(
            [{"codec_type": "data", "codec_tag_string": "gpmd",
              "tags": {"handler_name": "GoPro MET"}}]
        )
        self.assertTrue(present)

    def test_rejects_video_without_known_gyro_track(self) -> None:
        present, reason = self._probe(
            [{"codec_type": "video", "codec_tag_string": "avc1"}]
        )
        self.assertFalse(present)
        self.assertIn("no known gyro metadata", reason)


class CliProbeTests(unittest.TestCase):
    def test_accepts_working_cli(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout="Gyroflow v1.6.3\n", stderr=""
            )
            usable, detail = MODULE.probe_gyroflow_cli(Path("/fake/gyroflow"))
        self.assertTrue(usable)
        self.assertEqual(detail, "Gyroflow v1.6.3")

    def test_sigtrap_is_reported_as_the_sandboxed_app_store_build(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=-5, stdout="", stderr="")
            usable, detail = MODULE.probe_gyroflow_cli(Path("/fake/gyroflow"))
        self.assertFalse(usable)
        self.assertIn("Mac App Store", detail)
        self.assertIn("brew install --cask gyroflow", detail)


class RenderJobTests(unittest.TestCase):
    def test_dry_run_does_not_create_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "new" / "clip_stabilized.mp4"
            success, message = MODULE.render_job(
                [gyroflow_stage(destination)], destination, dry_run=True
            )
            self.assertTrue(success)
            self.assertTrue(message.startswith("DRY RUN:"))
            self.assertFalse(destination.parent.exists())

    def test_exit_zero_without_output_is_a_failure(self) -> None:
        """Gyroflow exits 0 even when it fails, so absence of output decides."""
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_stabilized.mp4"
            with mock.patch.object(MODULE.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0)
                success, message = MODULE.render_job(
                    [gyroflow_stage(destination)], destination, dry_run=False
                )
        self.assertFalse(success)
        self.assertIn("no output from Gyroflow", message)

    def test_success_requires_a_non_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_stabilized.mp4"

            def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
                destination.write_bytes(b"rendered")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
                success, message = MODULE.render_job(
                    [gyroflow_stage(destination)], destination, dry_run=False
                )
        self.assertTrue(success)
        self.assertTrue(message.startswith("OK:"))

    def test_timeout_reports_failure_and_removes_partial_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_stabilized.mp4"
            partial = destination.with_name(destination.name + ".tmp")

            def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
                partial.write_bytes(b"half a render")
                raise subprocess.TimeoutExpired(cmd="gyroflow", timeout=5)

            with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
                success, message = MODULE.render_job(
                    [gyroflow_stage(destination)], destination, dry_run=False, timeout=5
                )

            self.assertFalse(success)
            self.assertIn("timed out", message)
            self.assertFalse(partial.exists())

    def test_failed_render_removes_partial_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "clip_stabilized.mp4"
            partial = destination.with_name(destination.name + ".tmp")

            def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
                partial.write_bytes(b"half a render")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
                success, _ = MODULE.render_job([gyroflow_stage(destination)], destination, dry_run=False)

            self.assertFalse(success)
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
