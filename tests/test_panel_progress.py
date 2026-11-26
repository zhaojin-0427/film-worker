"""Keep panel timing separate from the worker heartbeat schema."""
import json
import tempfile
import unittest
from pathlib import Path

from film_worker.progress import Progress, panel_details


class PanelProgressTests(unittest.TestCase):
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
