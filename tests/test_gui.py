from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
