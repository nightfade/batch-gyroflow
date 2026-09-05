# Gyroflow Batch Stabilization

`batch_gyroflow.py` recursively finds video files and processes them in one of
three modes. Source videos are never modified; output is written below an
excluded `gyroflow_stabilized/` folder, preserving the source tree.

| Mode | Flags | Pipeline |
|---|---|---|
| Stabilize | *(default)* | Gyroflow → delivery |
| Stabilize + LUT | `--lut grade.cube` | Gyroflow → ProRes → ffmpeg LUT → delivery |
| LUT only | `--lut grade.cube --lut-only` | ffmpeg LUT → delivery |

No lens profile or external gyro file is supplied, so Gyroflow uses each clip's
embedded motion metadata and its automatic handling.

In LUT-only mode Gyroflow is never launched, no staging file is written, and
the gyro-metadata probe is skipped — so it works on any footage and does not
need Gyroflow installed at all.

## GUI

```bash
./build_app.sh              # installs BatchGyroflow.app into /Applications
```

Double-clicking it starts a loopback web server and opens the page in your
browser. It exposes mode, source, output, LUT, max crop and date naming, with
codec / bitrate / jobs / timeout / rolling-shutter behind **高级选项**, plus a
live log, per-file progress and a Stop button.

Build it on each Mac rather than copying the bundle — it takes a second, needs
only the system tools, and avoids the Gatekeeper prompt a copied ad-hoc-signed
app would trigger. The bundle does not embed the scripts; it points at this
directory, so editing `batch_gyroflow.py` takes effect immediately.

`gui.py` also runs standalone (`python3 gui.py`) if you would rather not
install an app.

Notes:

- Standard library only. Homebrew's Python has no tkinter and there is no Xcode
  here, so a browser page is what needs nothing installed.
- Folder and LUT pickers are the real macOS dialogs: a browser cannot see local
  paths, so the server shells out to `osascript` and returns the choice.
- The server binds to 127.0.0.1 and every request needs a per-run token, so
  another page in the same browser cannot drive it.
- Progress percentages come from Gyroflow's own output. The ffmpeg LUT stage
  reports none, so it shows an indeterminate bar.
- Stop sends SIGTERM to the whole process group; `batch_gyroflow.py` handles it
  through the same path as Ctrl-C, so partial `.tmp` files are cleaned up.

## Requirements

| | macOS | Linux |
|---|---|---|
| Gyroflow | `brew install --cask gyroflow` | AppImage from gyroflow.xyz, pass `--gyroflow` |
| `ffprobe` | `brew install ffmpeg` | distro `ffmpeg` |
| Python | 3.9+ | 3.9+ |

**The Mac App Store build of Gyroflow does not work.** It ships with
`com.apple.security.app-sandbox`, so its CLI aborts in
`_libsecinit_appsandbox` before `main()` runs (SIGTRAP / exit 133), and it can
only read files chosen through a dialog. Install the Homebrew or gyroflow.xyz
build instead. The script runs `gyroflow --version` before starting and reports
this specific cause; `--skip-cli-check` bypasses the probe.

## Before recording

- Turn camera stabilization off when recording for later gyro stabilization.
- If a lens has optical stabilization, turn that off as well. Moving optical
  elements are not fully described by the body gyro metadata.
- Electronic lenses provide useful focal-length metadata. Manual or unsupported
  lenses may still require intervention inside Gyroflow.

## Dry run

```bash
python3 batch_gyroflow.py \
  "/path/to/videos" --dry-run
```

## Render

```bash
python3 batch_gyroflow.py \
  "/path/to/videos" --output-dir "/path/to/stabilized"
```

Defaults: H.265/HEVC at 100 Mbps, GPU encoding, audio preserved, one render at a
time, 2 hour per-file timeout. `--bitrate 0` lets Gyroflow scale the bitrate by
resolution instead (roughly 1 Mbps at 360p30, 28 Mbps at 1080p60 — usually too
low for delivery, which is why an explicit default is set).

## Limiting the crop

Stabilization crops into the frame. `--max-zoom` caps how far:

```
max_zoom = 100 / (1 - crop)
```

| Max zoom | Largest crop per dimension |
|---|---|
| 100 | 0% (no crop; edges may be exposed) |
| 110 | 9.1% |
| 117.6 | 15% |
| 130 | 23.1% |

```bash
python3 batch_gyroflow.py "/path/to/videos" --max-zoom 117.6 --zoom-window -1
```

This is a ceiling, not a fixed amount: a steady clip may only crop 5%, while a
shaky one stops at the cap and keeps the remaining shake. `--zoom-window -1`
holds one constant crop for the whole clip; different clips still get different
amounts, so matching the field of view across a timeline needs a fixed FOV.

Omit both flags to use whatever is set in the Gyroflow GUI (stored in
`~/Library/Application Support/Gyroflow/settings.json`).

## Recording date in the output filename

`--add-date` puts the source clip's recording date into the output name, so a
stabilized file still says when it was shot:

```bash
python3 batch_gyroflow.py "/path/to/videos" --add-date prefix
# 2026-08-20_shot_stabilized.mp4

python3 batch_gyroflow.py "/path/to/videos" --add-date suffix
# shot_2026-08-20_stabilized.mp4
```

Either way the render suffix stays last, so outputs are still recognised as
outputs and re-runs skip them.

`--date-format` takes any strftime pattern (default `%Y-%m-%d`; use
`%Y%m%d_%H%M%S` to include the time). Patterns that render empty or contain a
path separator are rejected.

The date is looked up in this order:

1. `com.apple.quicktime.creationdate` — carries the shooting UTC offset, so its
   wall clock is used exactly as written.
2. `creation_time` — normally UTC, converted to this machine's local time.
   Pass `--date-utc` to keep UTC instead. This is right when you edit in the
   same timezone you shot in, and off by a day near midnight when you don't.
3. The file's modification time, reported as a `NOTE` so you know it is a guess.

Timestamps before 1980 are ignored (QuickTime's zero epoch is 1904, and cameras
with an unset clock emit 1970). If nothing usable is found the name is left
undated with a warning rather than failing.

## Applying a LUT

Gyroflow has no LUT support of any kind — no `lut` keys anywhere in the binary,
no colour section in its project schema. So `--lut` runs an ffmpeg pass:

```bash
# stabilize, then bake the LUT
python3 batch_gyroflow.py "/path/to/videos" --lut ~/luts/slog3_to_709.cube

# only bake the LUT, no stabilization
python3 batch_gyroflow.py "/path/to/videos" --lut ~/luts/slog3_to_709.cube \
  --lut-only --suffix _graded
```

When stabilizing *and* grading, Gyroflow stages the clip as ProRes 422 (10-bit
4:2:2, visually lossless) and ffmpeg produces the delivery encode from it, so
there is only one lossy generation. The staging file is deleted afterwards;
`--keep-intermediate` keeps it.

**Peak disk use is several times the delivery size** — a 2 s 720p clip staged
at 12 MB against a 780 KB source. With `--jobs N` there are N staging files
alive at once, so leave headroom. LUT-only mode writes no staging file.

If the LUT pass fails, the Gyroflow render is *not* discarded: the ProRes is
kept and the error names it, so only the cheap half is re-run.

`--lut-encoder` overrides the encoder (default: VideoToolbox on macOS,
libx264/libx265 elsewhere). The chain converts to `yuv444p16le` before `lut3d`
so gradients do not band, then back to `yuv420p`.

### Colour tagging

The source's `color_range`, `colorspace`, `color_primaries` and `color_trc` are
probed and reproduced on the output. This matters: `lut3d` works on the RGB that
results from the tagged range, so a limited/full mismatch feeds the LUT the
wrong signal. A near-linear test LUT shifted output by one code value between
`tv` and `pc` tagging; a real log→Rec709 LUT has far steeper curves and the
error grows with them.

### What is and is not verified

The LUT arithmetic is exact. Feeding a flat `(200,200,200)` source through a
halve-green / zero-blue LUT in **LUT-only** mode produced exactly
`(200,100,0)`.

The same source through Gyroflow did **not** round-trip its levels: 200 came
back as 203 rendering straight to H.265, and as 188 via the ProRes staging
path. That shift comes from Gyroflow's own encode, not the LUT pass — LUT-only
mode is the control that isolates it. The test source was tagged
`color_range=unknown`, which real cameras do not produce, so this may not
affect real footage. It has not been checked against a real camera clip.

Before committing a batch, verify one clip:

```bash
python3 batch_gyroflow.py "/one/clip/folder" --lut grade.cube --keep-intermediate
```

and compare the ProRes staging file against the delivery file in your viewer.

## Rolling shutter (jello) correction

Gyroflow corrects rolling shutter skew as part of the same pass as
stabilization — it warps each scanline according to where the gyro says the
camera was pointing when that line was read out. It needs one number to do
that: the sensor's **frame readout time** in milliseconds.

**It is off by default.** An exported Gyroflow project shows
`frame_readout_time = 0.0`, and only **1063 of the 9750 bundled lens profiles
(10.9%)** supply a value — Sony 15.7%, Canon 13.8%, GoPro 8.1%, RED 0.5%. When
the profile has nothing, the value stays 0 and no correction happens, silently.

```bash
python3 batch_gyroflow.py "/path/to/videos" --readout-time 15.6
```

Across the profiles that do carry a value, readout times run 0.01–100 ms with a
median of 15.6 ms. Getting it right per camera/mode matters: too low leaves
skew, too high over-corrects the other way.

Three ways to find the value for a camera:

1. Open one clip in the Gyroflow GUI, pick **rs-sync** as the synchronization
   method, and run a sync — it estimates the readout time from the footage.
   Then reuse that number here for the whole batch.
2. Check whether the bundled lens profile already has it (if so, nothing to do).
3. Measure or look it up, then confirm by eye on a fast horizontal pan.

`--readout-direction` accepts `TopToBottom` (default), `BottomToTop`,
`LeftToRight`, `RightToLeft`. Gyroflow silently falls back to `TopToBottom` for
anything else.

## Gyro metadata probe

Before queuing, `ffprobe` looks for a known timed-metadata track: Sony `rtmd`,
GoPro `gpmd` / "GoPro MET", or CAMM. Unrecognised files are **rendered anyway**
with a warning, because the probe does not cover every camera. Use
`--require-gyro-metadata` to skip them instead, or `--skip-gyro-check` to drop
the probe entirely.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Gyroflow CLI behaviour this script works around

Verified against Gyroflow 1.6.3 on macOS 26.6.2:

- **Exit codes are meaningless.** Gyroflow returns 0 even when the input does
  not exist or the render fails, and it logs `[ERROR]` lines on successful
  renders too. Only the presence of the output file is trustworthy.
- **Renders go to `<output>.tmp` and are renamed on completion.** An aborted
  render leaves a large partial `.tmp`, which the script removes on failure,
  timeout, and `Ctrl-C`.
- **`--preset` cannot be repeated** ("duplicate values provided"), so a preset
  file and the inline overrides are merged into one JSON value.
- **Unknown preset keys are accepted and silently ignored.** Names must match
  the project schema exactly: `adaptive_zoom_window` works, `adaptive_zoom`
  parses without complaint and does nothing. Check any new key by running
  `gyroflow clip.MP4 --export-project 1 --preset '<json>'` and reading the
  resulting `.gyroflow` file.
- **A corrupt input can hang Gyroflow indefinitely** (a 5 KB junk `.MP4` ran
  past 9 minutes), hence `--timeout`.
- **The output extension follows the codec, not the input.** A `.MOV` source
  produces `.mp4`. The script sidesteps this by passing an explicit
  `output_path` in `--out-params`.
