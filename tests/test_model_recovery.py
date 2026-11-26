"""Exercise subprocess restart with the same Codex session."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from film_worker import model_recovery
from film_worker.settings import atomic_json


SESSION = "01a21221-a32e-7d93-8dc7-6f6949707bf5"


class ModelRecoveryTests(unittest.TestCase):
    def test_active_tools_and_normal_silence_are_not_reconnects(self):
        output = model_recovery.CodexOutput(SESSION)
        output.consume(b"exec", 1)
        output.consume(b"exec", 2)
        output.consume(b"ERROR: Reconnecting... waiting for network", 3)
        output.consume(b"ERROR: Reconnecting... waiting for network", 30)
        self.assertEqual(output.pending, 2)
        self.assertEqual(output.retry_since, 3)
        output.consume(b" succeeded in 20ms:", 40)
        output.consume(b" succeeded in 20ms:", 41)
        self.assertEqual(output.pending, 0)
        self.assertIsNone(output.retry_since)
        self.assertIsNotNone(output.last_activity)

    def test_stalled_cli_restarts_in_same_directory_and_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prompt = root / "prompt.txt"
            prompt.write_text("resume the same work", encoding="utf-8")
            plan = root / "shot-plan.json"
            plan.write_text("saved input", encoding="utf-8")
            monitor = model_recovery.CodexRecovery(root, "codex", "fixture")
            invocations = []

            def command(session):
                invocations.append(session)
                if session is None:
                    script = (
                        "import time\n"
                        f"print('session id: {SESSION}', flush=True)\n"
                        "print('ERROR: Reconnecting... waiting for network', flush=True)\n"
                        "time.sleep(30)"
                    )
                else:
                    script = (
                        "from pathlib import Path\n"
                        "assert input() == 'resume the same work'\n"
                        "assert Path('shot-plan.json').read_text() == 'saved input'\n"
                        f"print('session id: {SESSION}')\n"
                        "print('model: fixture', 'provider: fixture', 'reasoning effort: high', sep=chr(10))\n"
                        "print('codex', 'completed the same work', sep=chr(10))"
                    )
                return [sys._base_executable, "-c", script]

            with patch.multiple(model_recovery, STALL_SECONDS=0.1, POLL_SECONDS=0.05, BACKOFF=(0.01, 0.01)):
                actual = monitor.run(command, cwd=root, env=os.environ.copy(), prompt=prompt,
                                     resume_prompt=prompt, log_path=root / "codex.log")
            self.assertEqual(invocations, [None, SESSION])
            self.assertEqual(actual["codex_model"], "fixture")
            saved = model_recovery.checkpoint(root)
            self.assertEqual(saved["used"], 1)
            self.assertEqual(saved["session_id"], SESSION)
            self.assertEqual(saved["state"], "completed")

    def test_operator_request_is_scoped_to_the_current_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = model_recovery.CodexRecovery(root, "codex-self-review", "fixture")
            monitor.publish("waiting", session_id=SESSION, reason="auth", wait_id="current")
            pending = model_recovery.location(root) / "retry.json"
            atomic_json(pending, {"wait_id": "obsolete"})

            def accept_after_first_poll(_):
                self.assertEqual(model_recovery.checkpoint(root)["state"], "waiting")
                atomic_json(pending, {"wait_id": "current"})

            with patch.object(model_recovery.time, "sleep", side_effect=accept_after_first_poll) as poll:
                monitor.wait_for_user("auth")
            poll.assert_called_once()
            state = model_recovery.checkpoint(root)
            self.assertEqual(state["state"], "resuming")
            self.assertIsNone(state["wait_id"])
            self.assertNotEqual(model_recovery.CodexRecovery(root, "codex-repair", "fixture").data["session_id"], SESSION)

    def test_exhausted_budget_survives_worker_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prompt = root / "prompt.txt"
            prompt.touch()
            monitor = model_recovery.CodexRecovery(root, "codex-repair", "fixture")
            monitor.publish("backoff", session_id=SESSION, used=2, reason="timeout", retry_at=0)
            recovered = model_recovery.CodexRecovery(root, "codex-repair", "fixture")
            invocations = []

            def command(session):
                invocations.append(session)
                return [sys.executable, "-c", "print('ERROR: Reconnecting... 5/5'); raise SystemExit(1)"]

            with patch.object(recovered, "wait_for_user", side_effect=InterruptedError) as waiting:
                with patch.object(model_recovery, "POLL_SECONDS", 0.04):
                    with self.assertRaises(InterruptedError):
                        recovered.run(command, cwd=root, env=os.environ.copy(), prompt=prompt,
                                      resume_prompt=prompt, log_path=root / "codex-repair.log")
            self.assertEqual(invocations, [SESSION])
            waiting.assert_called_once_with("connection")
            self.assertEqual(model_recovery.checkpoint(root)["used"], 2)

    def test_manual_deadline_expiry_preserves_the_scene(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene = root / "scene.blend"
            scene.write_bytes(b"existing scene")
            monitor = model_recovery.CodexRecovery(root, "codex", "fixture")
            monitor.publish("waiting", session_id=SESSION, reason="timeout", wait_id="late",
                            manual_deadline=time.time() - 1)
            atomic_json(monitor.folder / "retry.json", {"wait_id": "late"})
            with self.assertRaisesRegex(model_recovery.RecoveryTimeout, "人工接回等待超时"):
                monitor.wait_for_user("timeout")
            self.assertEqual(scene.read_bytes(), b"existing scene")
            self.assertEqual(model_recovery.checkpoint(root)["state"], "timed_out")

    def test_resume_requires_a_fresh_matching_lease_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = model_recovery.CodexRecovery(root, "codex", "fixture", lease_owner="current")
            previous = model_recovery.ResumeLease(monitor.folder, "previous")
            previous.grant("old-ticket", time.monotonic() + 60)
            prompt = root / "prompt.txt"
            prompt.touch()
            with patch.multiple(model_recovery, LEASE_CONFIRM_SECONDS=.04, POLL_SECONDS=.01):
                with self.assertRaisesRegex(model_recovery.RecoveryTimeout, "续租确认"):
                    monitor.run(lambda _: self.fail("Codex started without a fresh permit"), cwd=root,
                                env=os.environ.copy(), prompt=prompt, resume_prompt=prompt,
                                log_path=root / "codex.log", initial_session=SESSION)
