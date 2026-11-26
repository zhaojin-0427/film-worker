"""Local panel timings and observable production milestones."""
import json
import re
import time
from contextlib import suppress
from datetime import datetime, timezone

from .model_recovery import BACKOFF, checkpoint, clock_text, location, read_state
from .settings import atomic_json


class Progress:
    def __init__(self, directory):
        self.directory = directory
        self.stage = None

    def __call__(self, stage, completed=0, total=None, *, render_size=None):
        if stage != self.stage:
            with suppress(OSError):
                atomic_json(self.directory / "panel-progress.json", dict(
                    stage=stage, started_at=datetime.now(timezone.utc).isoformat(), render_size=render_size,
                ))
            self.stage = stage
        atomic_json(self.directory / "progress.json", dict(stage=stage, completed=completed, total=total))


CODEX_LOGS = {
    "codex_author": "codex.log",
    "codex_resume": "codex-resume.log",
    "codex_repair": "codex-repair.log",
    "codex_self_review": "codex-self-review.log",
    "codex_visual_repair": "codex-visual-repair.log",
    "repair_engineering": "codex-repair.log",
}
CODEX_PHASES = {stage: log.removesuffix(".log") for stage, log in CODEX_LOGS.items()}
CODEX_EVENTS = re.compile(
    rb"(?m)^(?:(?P<reconnecting>ERROR: Reconnecting[^\r\n]*)|(?P<tool>exec\r?$)|"
    rb"(?P<tool_result> (?:succeeded in|exited )[^\r\n]*))"
)


def modified(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def panel_details(directory, status, kind):
    stage = status["stage"]
    try:
        saved = json.loads((directory / "panel-progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    details = {}
    if isinstance(saved, dict) and saved.get("stage") == stage:
        started = saved.get("started_at")
        if isinstance(started, str):
            details["started_at"] = started
        dimensions = saved.get("render_size")
        if isinstance(dimensions, list) and len(dimensions) == 2 and all(type(axis) is int for axis in dimensions):
            details["render_size"] = dimensions
    if not details.get("started_at") and status["total"] is None:
        details["started_at"] = modified(directory / "progress.json")

    visual = stage in {"preview_visual_review", "preview_auditor", "codex_auditor"} or stage.startswith("render_review_")
    if visual and (activity := read_state(directory / "visual-progress.json")):
        details["visual_review"] = activity
    auditor = visual and "visual_review" in details
    recovery = read_state(location(directory) / "auditor/state.json") if auditor else checkpoint(directory)
    expected_phase = "codex-auditor" if auditor else CODEX_PHASES.get(stage)
    if expected_phase and recovery.get("phase") == expected_phase and recovery["state"] != "completed":
        manual_deadline = recovery.get("manual_deadline") if recovery["state"] == "waiting" else None
        details["recovery"] = {
            "state": recovery["state"], "reason": recovery.get("reason"),
            "used": recovery["used"], "limit": len(BACKOFF),
            "last_activity": recovery.get("last_activity"),
            "next_retry_at": clock_text(recovery["retry_at"]) if recovery.get("retry_at") else None,
            "manual_deadline": clock_text(manual_deadline) if manual_deadline else None,
            "wait_id": recovery.get("wait_id") if recovery["state"] == "waiting"
                       and (manual_deadline is None or time.time() < manual_deadline)
                       and recovery["session_id"] and recovery.get("reason") != "missing_session" else None,
        }

    if stage not in CODEX_LOGS:
        return details
    log_path = directory / CODEX_LOGS[stage]
    try:
        with log_path.open("rb") as log:
            length = log.seek(0, 2)
            log.seek(max(0, length - 64 * 1024))
            tail = log.read(64 * 1024)
        updated = modified(log_path)
    except OSError:
        updated = None
    if updated and (not details.get("started_at") or updated >= details["started_at"]):
        activity = "model_output"
        for event in CODEX_EVENTS.finditer(tail):
            activity = event.lastgroup
        details.update(activity=activity, log_updated_at=updated)

    if kind == "prepare":
        scene = directory / "scene"
        milestones = []
        for name, milestone in (
            ("shot-plan.json", "shot_plan"),
            (".draft-inputs", "compiled"),
            ("首版自检对比-当前工程.mp4", "draft_saved"),
            (".draft-review.json", "draft_checked"),
        ):
            if at := modified(scene / name):
                milestones.append(dict(step=milestone, at=at))
        details["milestones"] = sorted(milestones, key=lambda event: event["at"], reverse=True)[:3]
    return details
