"""Local configuration and capability discovery."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import av
import PIL
import rfc8785

from .protocol import opaque


ROOT = Path(__file__).resolve().parent.parent
KINDS = ["prepare", "revise", "finalize"]
UPDATE_REMOTE = "https://github.com/zgw494294-blip/film-worker.git"
MODEL_FIELDS = ("codex_model", "codex_provider", "reasoning_effort")
REVIEW_JPEG = {"fine": (94, 0), "standard": (85, 2), "economy": (78, 2)}
REVIEW_FIELDS = ("review_clarity", "dual_role_review")

DEFAULTS = {
    "server": "",
    "node_key": "",
    "display_name": platform.node(),
    "slots": 1,
    "model_slots": 1,
    "render_slots": 1,
    "transfer_slots": 1,
    "cpu_threads": 4,
    "disk_reserve_gib": 8,
    "final_width": 1920,
    "review_clarity": "standard",
    "dual_role_review": True,
    "codex": "",
    "blender": "",
    "ffmpeg": "",
    "ffprobe": "",
    "render_backend": "auto",
    "codex_model": "",
    "codex_provider": "",
    "reasoning_effort": "",
    "update_remote": UPDATE_REMOTE,
    "update_tag": "stable",
    "update_signers": "",
    "update_interval_minutes": 30,
    "update_window_start": "",
    "update_window_end": "",
    "auto_update": True,
}


def data_root():
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Film Worker"
    return home / "Library/Application Support/Film Worker"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def object_hash(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def bundled_release():
    bootstrap = Path(os.environ.get("FILM_WORKER_BOOTSTRAP", ROOT))
    release = bootstrap / "release.json"
    if (bootstrap / "runtime/build.json").is_file() and release.is_file():
        return bootstrap, json.loads(release.read_text(encoding="utf-8"))
    return None


def load(path):
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    bootstrap, manifest = bundled_release() or (ROOT, {})
    signers = bootstrap / "release.allowed_signers"
    if not settings.get("update_remote"):
        settings["update_remote"] = manifest.get("update_remote") or UPDATE_REMOTE
    if not settings.get("update_signers") and signers.is_file():
        settings["update_signers"] = str(signers)
    return validate_config(settings)


def validate_config(settings):
    legacy = settings.get("profiles", [])
    model = legacy[0] if isinstance(legacy, list) and legacy and isinstance(legacy[0], dict) else {}
    config = {**DEFAULTS, **{k: model[k] for k in MODEL_FIELDS if k in model},
              **{k: v for k,v in settings.items() if k in DEFAULTS}}
    if "final_width" in model and "final_width" not in settings:
        config["final_width"] = model["final_width"]
    for field in ("slots", "model_slots", "render_slots", "transfer_slots", "cpu_threads", "disk_reserve_gib"):
        if type(config[field]) is not int or not 1 <= config[field] <= 128:
            raise ValueError(f"{field} 须为 1–128 的整数")
    if type(config["final_width"]) is not int or not 2 <= config["final_width"] <= 3840:
        raise ValueError("final_width 须为 2–3840 的整数")
    if not isinstance(config["review_clarity"], str) or config["review_clarity"] not in REVIEW_JPEG:
        raise ValueError("未知审片清晰度")
    if type(config["dual_role_review"]) is not bool:
        raise ValueError("双角色自动送审设置无效")
    interval = config["update_interval_minutes"]
    if type(interval) is not int or not 1 <= interval <= 1440 or type(config["auto_update"]) is not bool:
        raise ValueError("更新周期须为 1–1440 分钟")
    for field in ("server", "node_key", "display_name", "codex", "blender", "ffmpeg", "ffprobe",
                  "update_remote", "update_tag", "update_signers", "update_window_start", "update_window_end",
                  *MODEL_FIELDS):
        if not isinstance(config[field], str) or "\0" in config[field]:
            raise ValueError(f"{field} 格式错误")
    start,end = config["update_window_start"],config["update_window_end"]
    if (start or end) and (start==end or not all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", item)
                                               for item in (start,end))):
        raise ValueError("自动更新时段需填写不同的开始、结束时间（HH:MM）")
    if any(config[k] and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", config[k]) for k in MODEL_FIELDS):
        raise ValueError("Codex 模型、服务商或推理强度格式错误")
    if config["render_backend"] not in {"auto", "cpu", "metal", "cuda", "optix", "hip", "oneapi"}:
        raise ValueError("未知 Blender 计算后端")
    return config


def windows_desktop_codex():
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return None
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$app=Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($app) { [Console]::WriteLine((Join-Path $app.InstallLocation 'app\\resources\\codex.exe')) }"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, check=True, timeout=10, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        path = Path(result.stdout.strip())
        if path.is_absolute() and path.is_file():
            return str(path)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return None


def resolve_tools(config):
    mac = {
        "blender": ["/Applications/Blender.app/Contents/MacOS/Blender"],
        "codex": ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"],
    }
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    roaming = Path(os.environ.get("APPDATA", str(Path.home()/"AppData/Roaming")))
    npm_roots = [roaming/"npm"]
    shim = shutil.which("codex.cmd") if os.name == "nt" else None
    if shim:
        npm_roots.append(Path(shim).parent)
    npm_candidates = []
    if os.name == "nt":
        machine = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        target = "aarch64" if machine == "arm64" else "x86_64"
        for path in npm_roots:
            vendor = path/("node_modules/@openai/codex/node_modules/@openai/codex-win32-"+machine)
            vendor = vendor/f"vendor/{target}-pc-windows-msvc"
            npm_candidates.extend([vendor/"bin/codex.exe",vendor/"codex/codex.exe"])
    windows = {
        "codex": [os.getenv("CODEX_CLI_PATH"), *npm_candidates],
        "blender": sorted((program_files / "Blender Foundation").glob("Blender */blender.exe"), reverse=True),
    }
    sites = windows if os.name == "nt" else mac
    tools = {}
    bundled = os.environ.get("FILM_WORKER_BUNDLED_TOOLS")
    for name in ("codex", "blender", "ffmpeg", "ffprobe"):
        filename = name + ".exe" if os.name == "nt" else name
        packaged = Path(bundled) / filename if bundled else None
        candidates = [config[name]] if config[name] else [packaged, shutil.which(name), *sites.get(name, [])]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                if os.name == "nt" and Path(candidate).suffix.lower() != ".exe":
                    continue
                tools[name] = str(Path(candidate).resolve())
                break
        if name == "codex" and os.name == "nt" and not config[name] and name not in tools:
            if desktop := windows_desktop_codex():
                tools[name] = desktop
        if name not in tools:
            raise ValueError(f"找不到 {name}，请设置可执行文件路径")
    return tools


def version(path, pattern, flag="--version"):
    args = [path, flag]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=20, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.SubprocessError as exc:
        raise ValueError(f"{Path(path).name} 版本检测失败") from exc
    match = re.search(pattern, result.stdout)
    if not match:
        raise ValueError(f"无法读取版本 {Path(path).name}")
    return match.group(1)


def local_build():
    active = os.environ.get("FILM_WORKER_RELEASE")
    if not active:
        manifest = ROOT / "runtime" / "build.json"
        if manifest.is_file():
            active = json.loads(manifest.read_text(encoding="utf-8"))["commit"]
        elif (ROOT / ".git").exists():
            try:
                result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                                        capture_output=True, text=True, timeout=5, check=True)
                active = result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return None
    return active if active and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", active) else None


def probe_render_backend(blender, backend):
    if backend in {"auto", "cpu"}:
        return
    device_type = backend.upper()
    script = (
        "import bpy; "
        "preferences=bpy.context.preferences.addons['cycles'].preferences; "
        "preferences.get_devices(); "
        f"devices=list(preferences.get_devices_for_type({device_type!r}) or []); "
        f"gpu=[device.name for device in devices if device.type == {device_type!r}]; "
        "print('FILM_WORKER_GPU=' + '|'.join(gpu)); "
        "raise SystemExit(0 if gpu else 17)"
    )
    try:
        result = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-expr", script, "--python-exit-code", "1"],
            capture_output=True, text=True, errors="replace", timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.SubprocessError as exc:
        raise ValueError(f"Blender {device_type} GPU 探测失败") from exc
    output = result.stdout + result.stderr
    match = re.search(r"FILM_WORKER_GPU=([^\r\n]*)", output)
    if result.returncode or not match or not match.group(1).strip():
        detail = match.group(1).strip() if match else "未返回设备名称"
        raise ValueError(f"Blender 未找到可用的 {device_type} GPU（{detail}）")


def inspect_tools(tools, render_backend="cpu"):
    ffmpeg = version(tools["ffmpeg"], r"ffmpeg version ([^\s]+)", "-version")
    probe = version(tools["ffprobe"], r"ffprobe version ([^\s]+)", "-version")
    build = version(tools["blender"], r"Blender ([^\s]+)")
    cli = version(tools["codex"], r"codex-cli ([^\s]+)")
    if probe != ffmpeg:
        raise ValueError("FFmpeg 和 FFprobe 版本必须一致")
    probe_render_backend(tools["blender"], render_backend)
    result = {"blender": build, "ffmpeg": ffmpeg, "codex_cli": cli,
              "decoder_id": f"rgb8-pillow{PIL.__version__}-av{av.__version__}/1"}
    if commit := local_build():
        result["runner_commit"] = commit
    return result


def environment(config, versions, profile=None, evidence="configured", actual_backend=None):
    mac = platform.system() == "Darwin"
    operating_system = "macOS " + platform.mac_ver()[0] if mac else "Windows " + platform.version()
    backend = config["render_backend"]
    if backend == "auto":
        backend = "metal" if mac and platform.machine() == "arm64" else "cpu"
    source = {**(profile or {}), **{k: config[k] for k in MODEL_FIELDS if config.get(k)}} if evidence != "unknown" else {}
    return dict(os=operating_system, arch=platform.machine().lower(), gpu="auto", render_backend=actual_backend or backend,
                compatibility=dict(versions), model_evidence=evidence if all(source.get(k) for k in MODEL_FIELDS) else "unknown",
                **{field: source.get(field) or None for field in MODEL_FIELDS})


def production_settings(config, versions, order):
    legacy = order.get("profile") or {}
    result = {k: config.get(k, "") for k in MODEL_FIELDS}
    result.update(final_width=legacy.get("final_width", config["final_width"]),
                  review_landscape_limit=legacy.get("review_landscape_limit", {"width": 1920, "height": 1080}),
                  review_portrait_limit=legacy.get("review_portrait_limit", {"width": 1080, "height": 1920}),
                  compatibility=dict(versions),
                  production_policy=order.get("production_policy", legacy.get("production_policy", "codex_trial")))
    if fingerprint := legacy.get("profile_hash"):
        result["profile_hash"] = fingerprint
    return result


def registration(config, instance_id, versions):
    node_key = config.get("node_key")
    if node_key:
        opaque(node_key)
    env = environment(config, versions)
    available = sorted({"cpu", env["render_backend"]})
    result = dict(instance_id=instance_id, display_name=config["display_name"],
                  capabilities=dict(work_kinds=KINDS, slots=config["slots"], max_frame_width=3840,
                                    render_backends=available, environment=env, oss_delivery=True, oss_objects=True))
    if node_key:
        result["node_key"] = node_key
    return result
