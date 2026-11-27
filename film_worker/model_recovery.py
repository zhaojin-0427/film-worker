"""Resume a stalled Codex subprocess while the worker keeps its lease."""
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .model import evidence
from .processes import ProcessTree
from .settings import atomic_json as write_state

STALL_SECONDS = 180
BACKOFF = (30, 120)
POLL_SECONDS = 2
MANUAL_WAIT_SECONDS = 15 * 60
LEASE_CONFIRM_SECONDS = 60
PHASES = {"codex", "codex-resume", "codex-repair", "codex-self-review", "codex-auditor", "codex-visual-repair"}
SESSION = re.compile(rb"session id: ([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")
RETRY = re.compile(rb"^ERROR: Reconnecting\.\.\.(?:\s|$)")
AUTH = re.compile(rb"(?i)(?:unexpected status|http|status code)\s+(401|403)\b")
MISSING_SESSION = re.compile(rb"(?i)^(?:ERROR:|Error:).*no rollout found for thread")
TRANSPORT = re.compile(rb"(?i)stream disconnected|connection failed|error sending request|unexpected status (429|50[234]|52[024])\b")
REASONS = {
    "connection": "模型连接中断", "timeout": "模型请求超时", "rate_limit": "模型服务限流",
    "auth": "模型鉴权失败", "missing_session": "找不到可接回的原会话",
}


class RecoveryTimeout(RuntimeError):
    pass


def clock_text(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def location(root):
    work = root.parent
    if work.parent.name == "work":
        return work.parent.parent / "codex-resume" / work.name / root.name
    return root / ".codex-recovery"


def read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class ResumeLease:
    """An attempt-scoped permit issued only after a new server heartbeat."""
    def __init__(self, folder, owner):
        self.folder = folder
        self.owner = owner

    def pending(self):
        request = read_state(self.folder / "lease-request.json")
        permit = read_state(self.folder / "lease-permit.json")
        if (request.get("owner") == self.owner and isinstance(request.get("ticket"), str)
                and (permit.get("owner"), permit.get("ticket")) != (self.owner, request["ticket"])):
            return request["ticket"]
        return None

    def grant(self, ticket, deadline):
        write_state(self.folder / "lease-permit.json", {"owner": self.owner, "ticket": ticket, "deadline": deadline})

    def confirm(self):
        ticket = uuid.uuid4().hex
        write_state(self.folder / "lease-request.json", {"owner": self.owner, "ticket": ticket})
        limit = time.monotonic() + LEASE_CONFIRM_SECONDS
        while time.monotonic() < limit:
            permit = read_state(self.folder / "lease-permit.json")
            expiry = permit.get("deadline")
            if ((permit.get("owner"), permit.get("ticket")) == (self.owner, ticket)
                    and isinstance(expiry, (int, float)) and time.monotonic() < expiry):
                return
            time.sleep(min(POLL_SECONDS, max(0, limit - time.monotonic())))
        raise RecoveryTimeout("Codex 接回未收到当前租约的续租确认，工程已保留")


def checkpoint(root):
    data = read_state(location(root) / "state.json")
    if data.get("phase") in PHASES and data.get("state") in {
        "running", "resuming", "reconnecting", "backoff", "restored", "waiting", "completed", "timed_out", "checking_lease",
    } and type(data.get("used")) is int and data.get("used") >= 0:
        return data
    return {}


class CodexOutput:
    def __init__(self, session):
        self.session = session
        self.offset = 0
        self.pending = 0
        self.reply = False
        self.retry_since = None
        self.reason = None
        self.last_activity = None

    def read(self, log, start, now):
        log.seek(start + self.offset)
        while line := log.readline(256 * 1024):
            if not line.endswith(b"\n") and len(line) < 256 * 1024:
                break
            self.offset += len(line)
            self.consume(line.rstrip(b"\r\n"), now)

    def consume(self, line, now):
        if match := SESSION.fullmatch(line):
            self.session = str(uuid.UUID(match[1].decode("ascii")))
        if MISSING_SESSION.search(line):
            self.reason = "missing_session"
            return
        if line.startswith((b"ERROR:", b"Error:")) or b"codex_core::responses_retry:" in line:
            if AUTH.search(line):
                self.reason = "auth"
                return
            if RETRY.match(line) or TRANSPORT.search(line):
                if self.reason in {"auth", "missing_session"}:
                    return
                if self.retry_since is None:
                    self.retry_since = now
                    self.reason = "connection"
                if b"524" in line or b"504" in line:
                    self.reason = "timeout"
                elif b"429" in line:
                    self.reason = "rate_limit"
                return
        if line == b"codex":
            self.reply = True
        elif line == b"exec":
            self.pending += 1
            self.advanced()
        elif line.startswith((b" succeeded in ", b" exited ", b" failed in ")):
            self.pending = max(0, self.pending - 1)
            self.advanced()
        elif self.reply and line.strip():
            self.advanced()

    def advanced(self):
        self.reply = False
        self.last_activity = clock_text()
        self.retry_since = None
        self.reason = None


def recent_frame(directory, now):
    newest = now - STALL_SECONDS
    for path in directory.rglob("*.png"):
        try:
            changed = path.stat().st_mtime
        except OSError:
            continue
        if newest < changed <= now:
            newest = changed
    return newest if newest > now - STALL_SECONDS else None


@contextmanager
def codex_process(command, *, cwd, env, log, stdin):
    if os.name == "nt":
        tree = ProcessTree(command, cwd=cwd, env=env, log=log, stdin=stdin)
        try:
            yield tree.child, tree.has_children
        finally:
            tree.close(wait_for_children=True)
    else:
        with subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, stdout=log, stderr=subprocess.STDOUT) as child:
            try:
                yield child, lambda: False
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()


class CodexRecovery:
    def __init__(self, root, phase, input_hash, *, lease_owner=None, unattended=False, journal=None, context_hash=None):
        folder = location(root)
        self.folder = folder / journal if journal else folder
        self.lease = ResumeLease(folder, lease_owner) if lease_owner else None
        self.unattended = unattended
        saved = read_state(self.folder / "state.json") if journal else checkpoint(root)
        if saved.get("input_hash") != input_hash:
            saved = {}
        if saved.get("phase") == phase and saved.get("context_hash") == context_hash and saved.get("state") != "completed":
            self.data = saved
        else:
            self.data = dict(phase=phase, input_hash=input_hash, session_id=None, used=0, state="running",
                             context_hash=context_hash, sessions=saved.get("sessions", {}))

    def publish(self, state, **fields):
        self.data.update(state=state, **fields)
        if self.data.get("session_id"):
            self.data.setdefault("sessions", {})[self.data["phase"]] = self.data["session_id"]
        write_state(self.folder / "state.json", self.data)

    def wait_for_user(self, reason):
        if self.unattended:
            self.publish("timed_out", reason=reason, wait_id=None)
            raise RecoveryTimeout(f"Codex 自动接回未成功（{REASONS[reason]}），工程已保留")
        previous = self.data.get("wait_id") if self.data["state"] == "waiting" else None
        wait_id = previous or uuid.uuid4().hex
        deadline = self.data.get("manual_deadline") or time.time() + MANUAL_WAIT_SECONDS
        self.publish("waiting", reason=reason, retry_at=None, wait_id=wait_id, manual_deadline=deadline)
        while time.time() < deadline:
            request = read_state(self.folder / "retry.json")
            if reason != "missing_session" and self.data["session_id"] and request == {"wait_id": wait_id}:
                self.publish("resuming", wait_id=None, reason=None)
                return
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.time())))
        self.publish("timed_out", wait_id=None)
        raise RecoveryTimeout(f"Codex 人工接回等待超时（{REASONS[reason]}），工程已保留")

    def run(self, command, *, cwd, env, prompt, resume_prompt, log_path, initial_session=None, deadline=None, cancelled=None):
        session = self.data["session_id"] or initial_session
        if self.data["state"] == "waiting":
            self.wait_for_user(self.data["reason"])
        elif self.data["state"] == "backoff":
            self.wait_for_retry(self.data["retry_at"])
        elif self.data["state"] == "timed_out":
            raise RecoveryTimeout(f"Codex 人工接回等待超时（{REASONS[self.data['reason']]}），工程已保留")
        while True:
            if session and self.lease is not None:
                self.publish("checking_lease", session_id=session, retry_at=None)
                self.lease.confirm()
            self.publish("resuming" if session else "running", session_id=session, retry_at=None)
            args = command(session)
            with log_path.open("ab") as output, log_path.open("rb") as log:
                start = output.tell()
                parser = CodexOutput(session)
                with (resume_prompt if session else prompt).open("rb") as stdin:
                    with codex_process(args, cwd=cwd, env=env, log=output, stdin=stdin) as (child, has_children):
                        while True:
                            now = time.monotonic()
                            if (cancelled and cancelled.is_set()) or (deadline is not None and now >= deadline):
                                raise RecoveryTimeout("Codex 审核已取消或超过等待时限")
                            parser.read(log, start, now)
                            self.observe(parser)
                            code = child.poll()
                            ready = not has_children()
                            if code is not None and ready:
                                parser.read(log, start, now)
                                self.observe(parser)
                                break
                            if ready and not parser.pending and code is None:
                                if parser.reason in {"auth", "missing_session"}:
                                    break
                                if parser.retry_since is not None and now - parser.retry_since >= STALL_SECONDS:
                                    wall = time.time()
                                    frame = recent_frame(cwd, wall)
                                    if frame is None:
                                        break
                                    parser.retry_since = now - (wall - frame)
                                    parser.last_activity = max(parser.last_activity or "", clock_text(frame))
                            time.sleep(POLL_SECONDS)
                if code == 0:
                    log.seek(start)
                    actual = evidence(log.read(32768).decode("utf-8", "replace"))
                    self.publish("completed", reason=None, evidence=actual)
                    return actual
            session = parser.session
            reason = parser.reason
            if reason in {"auth", "missing_session"}:
                self.wait_for_user(reason)
            elif reason in {"connection", "timeout", "rate_limit"}:
                if session and self.data["used"] < len(BACKOFF):
                    next_at = time.time() + BACKOFF[self.data["used"]]
                    self.publish("backoff", reason=reason, retry_at=next_at, used=self.data["used"] + 1)
                    self.wait_for_retry(next_at)
                else:
                    self.wait_for_user(reason if session else "missing_session")
            else:
                raise RuntimeError(f"{self.data['phase']} 退出码 {code}，详见本机日志")

    def observe(self, parser):
        state = self.data["state"]
        if parser.reason in {"connection", "timeout", "rate_limit"}:
            state = "reconnecting"
        elif parser.last_activity:
            state = "restored" if self.data["used"] or self.data["state"] in {"resuming", "restored", "reconnecting"} else "running"
        fields = dict(session_id=parser.session, reason=parser.reason, last_activity=parser.last_activity or self.data.get("last_activity"))
        if state != self.data["state"] or any(self.data.get(key) != value for key, value in fields.items()):
            self.publish(state, **fields)

    @staticmethod
    def wait_for_retry(timestamp):
        while (remaining := timestamp - time.time()) > 0:
            time.sleep(min(POLL_SECONDS, remaining))
