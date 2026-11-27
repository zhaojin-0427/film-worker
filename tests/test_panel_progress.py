"""Keep panel timing separate from the worker heartbeat schema."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from film_worker.progress import Progress, panel_details
from film_worker.settings import atomic_json


class PanelProgressTests(unittest.TestCase):
    def test_progress_replace_retries_windows_sharing_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            progress = Progress(directory)
            denied = PermissionError("sharing violation")
            denied.winerror = 32
            replace = Path.replace
            attempts = []

            def replace_once_blocked(source, target):
                if target.name == "progress.json":
                    attempts.append(source)
                    if len(attempts) == 1:
                        raise denied
                return replace(source, target)

            with patch.object(Path, "replace", replace_once_blocked):
                self.assertTrue(progress("render_review_optix", 1, 130))
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], attempts[1])
            self.assertEqual(json.loads((directory / "progress.json").read_bytes())["completed"], 1)
            self.assertFalse(list(directory.glob("*.tmp")))

    def test_progress_keeps_last_snapshot_if_windows_still_denies_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "progress.json"
            progress = Progress(directory)
            progress("render_review_optix", 1, 130)
            denied = PermissionError("access denied")
            denied.winerror = 5
            with (patch.object(Path, "replace", side_effect=denied),
                  patch("film_worker.settings.time.sleep") as wait,
                  self.assertLogs("film_worker.progress", level="WARNING") as warning):
                self.assertFalse(progress("render_review_optix", 2, 130))
                self.assertEqual(wait.call_count, 3)
                with self.assertRaises(PermissionError):
                    atomic_json(directory / "outputs.json", {"required": True})
            self.assertIn("WinError 5", warning.output[0])
            self.assertEqual(json.loads(path.read_bytes())["completed"], 1)
            self.assertTrue(progress("render_review_optix", 3, 130))
            self.assertEqual(json.loads(path.read_bytes())["completed"], 3)
            self.assertFalse(list(directory.glob("*.tmp")))

    def test_render_stage_keeps_its_start_and_reports_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            progress = Progress(directory)
            progress("render_review_optix", 0, 918, render_size=(1920, 1060))
            status = json.loads((directory / "progress.json").read_text())
            first = panel_details(directory, status, "prepare")
            progress("render_review_optix", 436, 918)
            status = json.loads((directory / "progress.json").read_text())
            self.assertEqual(status, dict(stage="render_review_optix", completed=436, total=918))
            self.assertEqual(panel_details(directory, status, "prepare"), first)
            self.assertEqual(first["render_size"], [1920, 1060])
            progress("compare_review", 0, 918)
            status = json.loads((directory / "progress.json").read_text())
            self.assertNotIn("render_size", panel_details(directory, status, "prepare"))

    def test_running_codex_log_reports_activity_without_returning_log_text(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            status = dict(stage="codex_author", completed=0, total=None)
            (directory / "progress.json").write_text(json.dumps(status))
            log = directory / "codex.log"
            log.write_text("exec\nlocal command\n succeeded in 10ms:\nERROR: Reconnecting... 2/5\n",
                           encoding="utf-8")
            scene = directory / "scene"
            scene.mkdir()
            (scene / ".draft-inputs").touch()
            details = panel_details(directory, status, "prepare")
            self.assertEqual(details["activity"], "reconnecting")
            self.assertEqual(details["milestones"][0]["step"], "compiled")
            self.assertIn("started_at", details)
            self.assertNotIn("local command", json.dumps(details))

            with log.open("a", encoding="utf-8") as file:
                file.write("exec\nlocal command\n succeeded in 10ms:\n")
            self.assertEqual(panel_details(directory, status, "prepare")["activity"], "tool_result")
