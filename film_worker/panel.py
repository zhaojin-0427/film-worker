"""Loopback-only control panel."""
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import model, startup
from .model_recovery import location as recovery_location, write_state as write_recovery_state
from .progress import panel_details
from .releases import RESTART_FOR_UPDATE, Releases
from .scheduler import Scheduler, attempt_history
from .secrets import read_oss_keys, read_token, store_oss_keys, store_token
from .settings import REVIEW_FIELDS, atomic_json, inspect_tools, load, resolve_tools, validate_config
from .state import State, StateBusy


WEB = Path(__file__).with_name("web")


def update_allowed(config, when):
    start,end = config["update_window_start"],config["update_window_end"]
    if not start:
        return True
    clock = when.strftime("%H:%M")
    if start<end:
        return start<=clock<end
    return clock>=start or clock<end


class Panel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port, test_http):
        super().__init__(("127.0.0.1", port), Handler)
        self.root = root
        self.config = root / "config.json"
        self.scheduler = None
        self.runner = None
        self.lock = threading.Lock()
        self.csrf = secrets.token_urlsafe()
        self.instance = secrets.token_hex(16)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.test_http = test_http
        self.start_error = None
        self.updater = Releases(root)
        self.update_lock = threading.Lock()
        self.update_display = "待检查"
        self.update_halt = threading.Event()
        self.managed = bool(os.getenv("FILM_WORKER_LAUNCH"))
        self.restart = False

    def start(self):
        if self.runner and self.runner.is_alive():
            return
        token = read_token(self.root)
        if not token:
            raise ValueError("请先保存建模 Token")
        self.scheduler = Scheduler(load(self.config), self.root, token, test_http=self.test_http)
        self.runner = threading.Thread(target=self.scheduler.run, daemon=True)
        self.runner.start()

    def summary(self):
        release = self.updater.state()
        detail = dict(managed=self.managed, release=release["active"], update_status=release["error"] or self.update_display,
                      rollback_available=release["previous"] is not None)
        if self.scheduler and self.runner and self.runner.is_alive():
            return {**self.scheduler.summary(), **detail}
        state = self.scheduler.state if self.scheduler else None
        if state is None and (self.root / "worker.sqlite3").is_file():
            state = State(self.root)
        history = attempt_history(self.root, state) if state else []
        return dict(connection="stopped", active=[], history=history, draining=False, detail=self.start_error, **detail)

    def update(self, *, rollback=False):
        if not self.managed:
            raise ValueError("请通过启动器打开面板以启用升级")
        if not self.update_lock.acquire(blocking=False):
            raise ValueError("正在进行另一个更新操作")
        self.update_display = "准备回退" if rollback else "检查签名和依赖"
        threading.Thread(target=self.perform_update, kwargs={"rollback": rollback}, daemon=True).start()

    def perform_update(self, *, rollback):
        try:
            release = self.updater.state()
            candidate = release["previous"] if rollback else self.updater.stage(load(self.config))
            if candidate is None:
                self.update_display = "没有可用的更新"
                return
            with self.lock:
                runner = self.runner
                if runner and runner.is_alive():
                    self.scheduler.drain.set()
                    self.scheduler.stop_after_drain.set()
            self.update_display = "正在等待当前工单提交"
            if runner:
                runner.join()
            if self.update_halt.is_set():
                return
            self.updater.queue(candidate, rollback=rollback)
            self.restart = True
            self.shutdown()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.update_display = str(exc)
        finally:
            self.update_lock.release()

    def poll_updates(self):
        last_interval = 0
        next_check = 0
        while not self.update_halt.wait(1):
            try:
                config = load(self.config)
                interval = config["update_interval_minutes"] * 60
                if interval != last_interval:
                    next_check = time.monotonic() + interval
                    last_interval = interval
                if (config["auto_update"] and config["update_remote"] and
                        time.monotonic() >= next_check and not self.update_lock.locked() and
                        update_allowed(config,datetime.now())):
                    self.update()
                    next_check = time.monotonic() + interval
            except (ValueError, OSError):
                next_check = time.monotonic() + 60


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body, mime="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def permitted(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def local_reviews(self):
        scheduler = self.server.scheduler
        if not scheduler:
            return
        with scheduler.lock:
            active = list(scheduler.active.values())
        for current in active:
            directory = current["directory"]
            if scheduler.current(directory, current["progress"])["stage"] != "awaiting_local_review":
                continue
            try:
                data = json.loads((directory / "local-review.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            yield directory, data

    def do_GET(self):
        if not self.permitted():
            return self.reply(403, {"error": "请求来源无效"})
        path = urlsplit(self.path).path
        if path == "/api/instance":
            return self.reply(200, {"instance":self.server.instance})
        if path == "/api/login":
            try:
                return self.reply(200, {"enabled":startup.login_enabled(self.server.root)})
            except (ValueError,OSError) as exc:
                return self.reply(400, {"error":str(exc)})
        if path == "/api/status":
            try:
                return self.reply(200, self.server.summary())
            except StateBusy as exc:
                return self.reply(503, {"error": str(exc)})
        if path == "/api/local-review":
            return self.reply(200, {"reviews": [data for _, data in self.local_reviews()]})
        if path == "/api/local-review/sample":
            query = parse_qs(urlsplit(self.path).query)
            for directory, data in self.local_reviews():
                if query.get("work") != [data["work_id"]] or query.get("attempt") != [str(data["attempt"])]:
                    continue
                for raw in data["samples"]:
                    sample = Path(raw).resolve()
                    if (sample.parent == (directory / "local-review").resolve()
                            and query.get("frame") == [sample.name] and sample.is_file()):
                        return self.reply(200, sample.read_bytes(), "image/jpeg")
            return self.reply(404, {"error": "找不到复核样张"})
        if path == "/api/config":
            try:
                oss_configured = bool(read_oss_keys(self.server.root))
            except (ValueError, OSError):
                oss_configured = False
            return self.reply(200, {"config": load(self.server.config), "csrf": self.server.csrf,
                                    "oss_configured": oss_configured})
        assets = {"/": ("index.html", "text/html"), "/panel.css": ("panel.css", "text/css"),
                  "/panel.js": ("panel.js", "text/javascript")}
        if path not in assets:
            return self.reply(404, {"error": "页面不存在"})
        file, mime = assets[path]
        self.reply(200, (WEB / file).read_bytes(), mime+"; charset=utf-8")

    def do_POST(self):
        if (not self.permitted() or self.headers.get("Origin") != self.server.origin or
                self.headers.get("X-Panel-Key") != self.server.csrf):
            return self.reply(403, {"error": "请求来源无效"})
        node = self.server
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if not 0 < count <= 1024 * 1024:
                raise ValueError("配置数据过大")
            payload = json.loads(self.rfile.read(count))
            if self.path == "/api/login":
                if not node.managed:
                    raise ValueError("请通过启动器运行后设置登录启动")
                with node.lock:
                    startup.configure_login(node.root,payload.get("enabled"))
                    return self.reply(200, {"enabled":startup.login_enabled(node.root)})
            if self.path == "/api/model-test":
                if node.runner and node.runner.is_alive():
                    raise ValueError("请先暂停领单并停止节点，再验证模型")
                config = load(node.config)
                answer = model.test(resolve_tools(config)["codex"], config)
                return self.reply(200,answer)
            if self.path == "/api/codex-reconnect":
                scheduler = node.scheduler
                if not scheduler or not node.runner or not node.runner.is_alive():
                    raise ValueError("节点未运行")
                if not isinstance(payload.get("work_id"), str) or type(payload.get("attempt")) is not int:
                    raise ValueError("工单身份无效")
                with scheduler.lock:
                    active = scheduler.active.get((payload["work_id"], payload["attempt"]))
                    if not active or active["stop"].is_set():
                        raise ValueError("工单已停止或租约已失效")
                    directory = active["directory"]
                    progress = scheduler.current(directory, active["progress"])
                    recovery = panel_details(directory, progress, active["kind"]).get("recovery", {})
                    wait_id = recovery.get("wait_id")
                    if not wait_id or wait_id != payload.get("wait_id"):
                        raise ValueError("当前恢复状态已变化")
                    write_recovery_state(recovery_location(directory) / "retry.json", {"wait_id": wait_id})
                return self.reply(200, {"saved": True})
            if self.path == "/api/local-review":
                if payload.get("decision") not in {"approve", "reject"} or not payload.get("work_id"):
                    raise ValueError("本地复核需要 work_id 和 approve/reject 决定")
                for _, data in self.local_reviews():
                    fields = ("work_id", "attempt", "outputs_sha256")
                    if all(payload.get(key) == data[key] for key in fields):
                        approval = node.scheduler.local_approval_path(data["work_id"], data["attempt"])
                        atomic_json(approval,
                                    {**{key: data[key] for key in (*fields, "job_id")},
                                     "decision": payload["decision"]})
                        return self.reply(200, {"saved": True, "decision": payload["decision"]})
                raise ValueError("复核版本已变化，请重新打开样张")
            with node.lock:
                if self.path == "/api/config":
                    config = validate_config(payload["config"])
                    oss_keys = payload.get("oss_keys")
                    if node.runner and node.runner.is_alive():
                        prior = load(node.config)
                        changing_other = any(config[key] != prior[key] for key in config if key not in REVIEW_FIELDS)
                        if changing_other or payload.get("token") or oss_keys:
                            raise ValueError("运行中可以保存审片设置。其他设置需等待在途工单完成并停止节点")
                        with node.scheduler.lock:
                            atomic_json(node.config, config)
                            node.scheduler.cfg.update({key: config[key] for key in REVIEW_FIELDS})
                        return self.reply(200, {"saved": True})
                    if payload.get("token"):
                        store_token(node.root, payload["token"])
                    if oss_keys:
                        store_oss_keys(node.root, oss_keys["id"], oss_keys["secret"])
                    atomic_json(node.config, config)
                    return self.reply(200, {"saved": True})
                if self.path == "/api/probe":
                    config = load(node.config)
                    versions = inspect_tools(resolve_tools(config), config["render_backend"])
                    return self.reply(200, {"versions": versions})
                if self.path in ("/api/update", "/api/rollback"):
                    node.update(rollback=self.path == "/api/rollback")
                elif self.path == "/api/start":
                    if node.update_lock.locked():
                        raise ValueError("更新正在进行，请稍候")
                    node.start()
                    node.start_error = None
                elif self.path in ("/api/drain", "/api/resume", "/api/stop"):
                    if node.scheduler and node.runner.is_alive():
                        if self.path == "/api/drain":
                            node.scheduler.state.set("claims_paused", True)
                            node.scheduler.drain.set()
                        elif self.path == "/api/resume":
                            if node.scheduler.stop_after_drain.is_set():
                                raise ValueError("节点正在停止，请在停止后重新启动")
                            node.scheduler.state.set("claims_paused", False)
                            node.scheduler.drain.clear()
                        else:
                            if node.scheduler.summary()["active"] or not node.scheduler.drain.is_set():
                                raise ValueError("请先暂停领单并等待在途工单完成")
                            node.scheduler.stop_after_drain.set()
                    elif self.path != "/api/stop":
                        raise ValueError("请先启动节点")
                else:
                    return self.reply(404, {"error": "未知操作"})
            self.reply(200, node.summary())
        except StateBusy as exc:
            self.reply(503, {"error": str(exc)})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})


def serve(root, port, browser, *, test_http=False):
    with Panel(root, port, test_http) as server:
        if server.managed:
            # Confirm runtime, data format and panel bind before the launcher activates this release.
            from .selftest import check
            check(root)
            session = Path(os.environ["FILM_WORKER_LAUNCH"])
            atomic_json(session / "ready.json", {"port": server.server_port})
            deadline = time.monotonic() + 45
            while not (session / "activate.json").exists():
                if time.monotonic() >= deadline:
                    raise ValueError("启动器确认超时")
                time.sleep(0.1)
        if server.config.is_file():
            try:
                server.start()
            except (ValueError, OSError, StateBusy) as exc:
                server.start_error = str(exc)
        threading.Thread(target=server.poll_updates, daemon=True).start()
        marker = startup.endpoint_file(root)
        atomic_json(marker,{"port":server.server_port,"instance":server.instance})
        print(server.origin, flush=True)
        if browser:
            webbrowser.open(server.origin)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.update_halt.set()
            marker.unlink(missing_ok=True)
            if server.runner:
                server.scheduler.stop.set()
                server.runner.join()
        return RESTART_FOR_UPDATE if server.restart else 0
