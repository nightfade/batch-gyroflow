from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


GUI = Path(__file__).parents[1] / "gui.py"
SPEC = importlib.util.spec_from_file_location("gui", GUI)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ScriptTests(unittest.TestCase):
    def test_script_goes_to_output_runs_dir_and_tees_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "my clips"
            source.mkdir()
            script = MODULE.write_script({"source": str(source), "mode": "stabilize"})

            self.assertEqual(script.parent, source / "gyroflow_stabilized" / "_runs")
            self.assertTrue(script.name.startswith("run_") and script.suffix == ".sh")
            self.assertTrue(script.stat().st_mode & 0o100)
            text = script.read_text(encoding="utf-8")
            self.assertIn("set -o pipefail", text)
            self.assertIn("'" + str(source) + "'", text)  # path with a space is quoted
            self.assertIn(str(script.with_suffix(".log")), text)
            self.assertIn(str(script), MODULE.GENERATED)

    def test_dry_run_and_explicit_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = MODULE.write_script(
                {"source": str(root), "output": str(root / "out"), "dry_run": True}
            )
            self.assertEqual(script.parent, root / "out" / "_runs")
            self.assertTrue(script.name.startswith("dryrun_"))
            self.assertIn("--dry-run", script.read_text(encoding="utf-8"))

    def test_script_reports_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "x.log"
            body = MODULE.render_script(["sh", "-c", "echo boom; exit 3"], log, "test")
            script = Path(temporary) / "x.sh"
            script.write_text(body, encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)], capture_output=True, text=True, stdin=subprocess.DEVNULL
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("boom", log.read_text(encoding="utf-8"))
            self.assertIn("退出码 3", result.stdout)

    def test_only_generated_scripts_can_be_opened(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.open_generated("/etc/passwd", reveal=True)

    def test_missing_source_is_rejected_before_writing(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.write_script({"source": ""})


def _write_cube(path: Path, transform) -> None:
    size = 5
    lines = [f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rgb = transform(r / (size - 1), g / (size - 1), b / (size - 1))
                lines.append(" ".join(f"{value:.6f}" for value in rgb))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class LutLibraryTests(unittest.TestCase):
    def test_lists_cube_files_recursively_and_skips_appledouble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pack").mkdir()
            for name in ("B.cube", "pack/a.CUBE", "._B.cube", "notes.txt"):
                (root / name).write_text("x", encoding="utf-8")
            self.assertEqual(
                MODULE.list_luts(str(root)), [str(root / "B.cube"), str(root / "pack/a.CUBE")]
            )
            self.assertEqual(MODULE.list_luts(""), [])

    def test_clips_exclude_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "gyroflow_stabilized").mkdir()
            for name in ("C0001.MP4", "._C0001.MP4", "gyroflow_stabilized/C0001_stabilized.mp4"):
                (root / name).write_bytes(b"")
            self.assertEqual(MODULE.list_clips(str(root)), [str(root / "C0001.MP4")])

    def test_settings_round_trip_keeps_only_known_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original = MODULE.SETTINGS
            MODULE.SETTINGS = Path(temporary) / "nested" / "gui.json"
            try:
                self.assertEqual(MODULE.load_settings(), {})
                MODULE.save_settings({"lut_dir": "/luts", "other": 1})
                self.assertEqual(MODULE.load_settings(), {"lut_dir": "/luts"})
            finally:
                MODULE.SETTINGS = original


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clip = self.root / "C0001.MP4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:d=1",
             "-pix_fmt", "yuv420p", "-colorspace", "bt709", str(self.clip)],
            check=True,
        )
        self.invert = self.root / "invert lut.cube"  # space exercises filter escaping
        _write_cube(self.invert, lambda r, g, b: (1 - r, 1 - g, 1 - b))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _centre_rgb(self, jpeg: bytes) -> tuple[int, int, int]:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "-", "-vf", "crop=8:8,scale=1:1,format=rgb24",
             "-f", "rawvideo", "-"],
            input=jpeg, capture_output=True, check=True,
        ).stdout
        return raw[0], raw[1], raw[2]

    def test_lut_is_applied_to_the_frame(self) -> None:
        original = MODULE.render_preview(str(self.clip), 50, "", 360)
        graded = MODULE.render_preview(str(self.clip), 50, str(self.invert), 360)
        self.assertTrue(original.startswith(b"\xff\xd8") and graded.startswith(b"\xff\xd8"))
        red, green, blue = self._centre_rgb(original)
        self.assertGreater(red, 230)
        self.assertLess(max(green, blue), 25)
        red, green, blue = self._centre_rgb(graded)  # red inverted to cyan
        self.assertLess(red, 25)
        self.assertGreater(min(green, blue), 230)

    def test_rejects_non_cube_and_non_video(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.render_preview(str(self.clip), 50, str(self.root / "x.txt"), 360)
        with self.assertRaises(ValueError):
            MODULE.render_preview(str(self.invert), 50, "", 360)


if __name__ == "__main__":
    unittest.main()
