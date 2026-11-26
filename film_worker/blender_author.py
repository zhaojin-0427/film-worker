"""Start Blender for Codex scene authoring with a private background profile."""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def bundled_profile(binary):
    info = Path(binary).resolve().parent.parent / "Info.plist"
    if sys.platform == "darwin" and info.is_file():
        with info.open("rb") as source:
            version = plistlib.load(source).get("CFBundleShortVersionString", "")
        profile = Path(__file__).resolve().parent / "blender-config" / version / "userpref.blend"
        if profile.is_file():
            return profile
    return None


@contextmanager
def background_environment(binary):
    profile = bundled_profile(binary)
    if profile is None:
        yield dict(os.environ)
        return
    with tempfile.TemporaryDirectory(prefix="movie-blender-config-") as config:
        shutil.copyfile(profile, Path(config) / "userpref.blend")
        yield dict(os.environ, BLENDER_USER_CONFIG=config)


def run_windows(binary, arguments):
    """Run Blender without a console while preserving its diagnostics."""
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.NamedTemporaryFile(prefix="film-blender-", suffix=".log", delete=False) as capture:
        capture_path = Path(capture.name)
    try:
        with capture_path.open("wb") as output:
            result = subprocess.run([binary, *arguments], stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=no_window)
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            with capture_path.open("rb") as output:
                shutil.copyfileobj(output, stream)
            stream.flush()
        return result.returncode
    finally:
        capture_path.unlink(missing_ok=True)


def main(binary, arguments):
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if "-b" not in options and "--background" not in options:
        arguments = ["-b", *arguments]
    if bundled_profile(binary):
        if any(option.split("=", 1)[0] in ("--factory-startup", "-X", "--gpu-backend", "--debug-gpu-backend-no-fallback")
               for option in options):
            sys.exit("后台制作配置已经指定图形后端，请去掉强制恢复出厂或切换图形后端的参数。")
        with background_environment(binary) as env:
            return subprocess.run([binary, *arguments], env=env).returncode
    if os.name == "nt":
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if "--console-crash-handler" not in options:
            help_text = subprocess.run([binary, "--help"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       timeout=10, creationflags=no_window).stdout
            if b"--console-crash-handler" in help_text:
                arguments = ["--console-crash-handler", *arguments]
        return run_windows(binary, arguments)
    os.execv(binary, [binary, *arguments])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
