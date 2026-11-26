"""Build installable, offline Python runtimes for the supported targets."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "3.12.16"
UV = "0.13.7"
TARGETS = {
    "macos-arm64": ("macos-aarch64-none", "macosx_11_0_arm64", "f91a229093163eefca89ea1b220a221d64c3beade05d03519a6584d1797a5a5f"),
    "macos-x86_64": ("macos-x86_64-none", "macosx_10_12_x86_64", "fd04bf01ba5f2829f4f94ca8f0c24a743f21e73d75045613ed6fc2fd66fc7bda"),
    "windows-x86_64": ("windows-x86_64-none", "win_amd64", "2835e2ab397d0db86323b9975dce75ecd76aa9cfa0d6a2aea84734aae7f8c9b3"),
}


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def fetch(url, expected, cache):
    basename = PurePosixPath(urlsplit(url).path).name
    path = cache / basename
    if path.exists() and sha(path) == expected:
        return path
    temp = cache / (basename + "." + uuid.uuid4().hex + ".pending")
    try:
        with urlopen(url, timeout=35) as response, temp.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024*1024)
        if sha(temp) != expected:
            raise ValueError(f"下载摘要不符 {basename}")
        temp.replace(path)
        return path
    finally:
        temp.unlink(missing_ok=True)


def get_uv(cache, tag, expected, runtime, windows):
    basename = f"uv-{UV}-py3-none-{tag}.whl"
    wheel = cache / basename
    if not wheel.exists() or sha(wheel) != expected:
        with urlopen(f"https://pypi.org/pypi/uv/{UV}/json", timeout=20) as response:
            data = json.load(response)
        url = next(file["url"] for file in data["urls"] if file["filename"] == basename)
        wheel = fetch(url, expected, cache)
    command = "uv.exe" if windows else "uv"
    with zipfile.ZipFile(wheel) as zipped:
        member = next(p for p in zipped.namelist() if p.endswith("/scripts/" + command))
        (runtime / command).write_bytes(zipped.read(member))
        (runtime / command).chmod(0o755)
        licenses = runtime / "licenses/uv"
        licenses.mkdir(parents=True)
        for member in zipped.namelist():
            if ".dist-info/licenses/" in member and not member.endswith("/"):
                (licenses / PurePosixPath(member).name).write_bytes(zipped.read(member))


def get_python(target_id, cache, provided_path):
    if provided_path:
        return provided_path.resolve(strict=True)
    store = cache / "interpreters"
    store.mkdir(exist_ok=True)
    exact = "cpython-" + PYTHON + "-" + target_id
    subprocess.run(["uv", "python", "install", "--no-bin", "--no-registry", "--install-dir", str(store), exact],
                   check=True, timeout=240)
    return (store / exact).resolve(strict=True)


def package_wheels(runtimes, target, cache):
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    wheeldir = runtimes / "wheels"
    wheeldir.mkdir()
    lines = []
    sources = []
    for package in lock["package"]:
        if package["name"] == "film-worker" or (package["name"] == "pywin32" and not target.startswith("windows")):
            continue
        def score(file):
            name = PurePosixPath(urlsplit(file["url"]).path).name
            python, abi, platforms = name.removesuffix(".whl").rsplit("-", 3)[-3:]
            if "py3" in python.split(".") and abi == "none" and platforms == "any":
                return 0
            if not python.startswith("cp3") or not python[3:].isdigit():
                return -1
            minor = int(python[3:])
            if python != "cp312" and (abi != "abi3" or minor > 12):
                return -1
            if target.startswith("windows"):
                return minor if platforms == "win_amd64" else -1
            arch = target.removeprefix("macos-")
            candidates = [tag for tag in platforms.split(".") if tag.startswith("macosx_")]
            if any(tag.endswith("_" + arch) for tag in candidates):
                return minor + 20
            return minor if any(tag.endswith("_universal2") for tag in candidates) else -1

        best = max((score(file) for file in package["wheels"]), default=-1)
        options = [file for file in package["wheels"] if best >= 0 and score(file) == best]
        if len(options) != 1:
            raise ValueError(f"{target} 没有唯一可分发的 wheel {package['name']}")
        artifact = options[0]
        hexdigest = artifact["hash"].split(":", 1)[1]
        path = fetch(artifact["url"], hexdigest, cache)
        shutil.copyfile(path, wheeldir / path.name)
        lines.append(f"{package['name']}=={package['version']} --hash=sha256:{hexdigest}")
        sources.append({"name": package["name"], "version": package["version"], "url": artifact["url"],
                        "license": "参见对应 wheel 中的许可证"})
    (runtimes / "wheels.txt").write_text("\n".join(lines)+"\n", encoding="utf-8", newline="\n")
    return sources


def file_records(env):
    return [{"path": path.relative_to(env).as_posix(), "sha256": sha(path), "size": path.stat().st_size}
            for path in sorted(env.rglob("*")) if path.is_file() and not path.name.startswith(".")]


def bundle(args):
    if subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], check=True,
                      capture_output=True, text=True).stdout.strip():
        raise ValueError("构建可分发安装包前先提交源码，确保包内提交号准确")
    target = args.target
    target_id, uv_tag, uv_sha = TARGETS[target]
    windows = target.startswith("windows")
    cache = args.output / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    python = get_python(target_id, cache, args.python_root)
    with tempfile.TemporaryDirectory(prefix=f"film-portable-{target}-") as temp:
        project = Path(temp) / "film-worker"
        project.mkdir()
        for name in ("README.md", "pyproject.toml", "uv.lock", "install_runtime.py", "contract.openapi.yaml",
                     "启动执行端.cmd", "启动执行端.command"):
            shutil.copy2(ROOT / name, project / name)
        (project / "docs").mkdir()
        for filename in ("服务端同机审片接入.md", "服务端返修增量接入.md"):
            handoff = Path("docs") / filename
            shutil.copy2(ROOT / handoff, project / handoff)
        for name in ("release.json", "release.allowed_signers"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, project / name)
        batch = project / "启动执行端.cmd"
        batch.write_text(batch.read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n")
        shutil.copytree(ROOT / "film_worker", project / "film_worker",
                        ignore=shutil.ignore_patterns("._*", "__pycache__", ".DS_Store"))
        runtime = project / "runtime"
        runtime.mkdir()
        shutil.copytree(python, runtime / "python", ignore=shutil.ignore_patterns("._*", "__pycache__", ".DS_Store"),
                        ignore_dangling_symlinks=True)
        get_uv(cache, uv_tag, uv_sha, runtime, windows)
        origin = [{"name": "CPython", "version": PYTHON, "source": "uv managed python-build-standalone",
                   "license": "PSF，见 runtime/python 内置许可证"},
                  {"name": "uv", "version": UV, "source": f"https://pypi.org/project/uv/{UV}/", "license": "MIT or Apache-2.0"}]
        origin.extend(package_wheels(runtime, target, cache))
        active_commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"], check=True,
                                       capture_output=True, text=True).stdout.strip()
        (runtime/"build.json").write_text(json.dumps({"commit":active_commit}), encoding="utf-8")
        if args.tools:
            if not (args.tools / "components.json").is_file():
                raise ValueError("工具目录须提供 components.json")
            components = json.loads((args.tools/"components.json").read_text(encoding="utf-8"))
            for component in components:
                for name, checksum in component.get("binaries", {}).items():
                    if PurePosixPath(name).name != name or "\\" in name or sha(args.tools/name) != checksum:
                        raise ValueError(f"随包工具与构建清单不符 {name}")
            origin.extend(components)
            shutil.copytree(args.tools, runtime/"tools",ignore=shutil.ignore_patterns("._*", ".DS_Store", "__pycache__"))
        manifest = {"target": target, "schema": 1, "components": origin, "files": file_records(runtime)}
        (runtime / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        output = args.output / f"电影复刻执行端-{target}.zip"
        temporary = output.with_suffix(".partial")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for source in sorted(project.rglob("*")):
                    if source.is_file():
                        archive.write(source, str(Path(project.name) / source.relative_to(project)))
            with zipfile.ZipFile(temporary) as archive:
                if archive.testzip():
                    raise ValueError("安装包完整性检查失败")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        output.with_suffix(".zip.sha256").write_text(f"{sha(output)}  {output.name}\n", encoding="utf-8")
        print(output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--python-root", type=Path)
    parser.add_argument("--tools", type=Path, help="可选工具目录，含 components.json 和对应许可证")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    bundle(parser.parse_args())
