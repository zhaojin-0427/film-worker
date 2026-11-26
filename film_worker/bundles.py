"""Bounded ZIP extraction and content indexes for protocol artifacts."""
import json
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath

from .protocol import digest

MAX_EXPANDED = 400 * 1024**3
MAX_FILES = 500_000
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def safe_path(name):
    if any(not piece or piece in (".", "..") or piece[-1] in " ." or RESERVED.match(piece)
           or re.search(r'[<>:"\\|?*\x00-\x1f]', piece) for piece in name.split("/")):
        raise ValueError("归档文件路径不安全")
    return PurePosixPath(name)


def entries(root):
    return [dict(path=p.relative_to(root).as_posix(), size_bytes=p.stat().st_size, sha256=digest(p))
            for p in sorted(root.rglob("*")) if p.is_file() and p != root / "index.json"]


def build(root, index, output, *, content=None):
    if content is None:
        content = entries(root)
    if not content:
        raise ValueError("归档为空")
    temp = output.with_suffix(".partial")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("index.json", json.dumps(dict(index, files=content), ensure_ascii=False).encode())
        for file in content:
            archive.write(root / safe_path(file["path"]), file["path"])
    temp.replace(output)


def extract(path, destination, *, indexed):
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        available = min(MAX_EXPANDED, shutil.disk_usage(destination).free)
        if len(items) > MAX_FILES or sum(item.file_size for item in items) > available:
            raise ValueError("归档大小或文件数量超过上限")
        seen = set()
        for item in items:
            raw_name = item.orig_filename.rstrip("/") if item.is_dir() else item.orig_filename
            relative = safe_path(raw_name)
            folded = unicodedata.normalize("NFC", raw_name).casefold()
            if folded in seen or item.flag_bits & 1:
                raise ValueError("归档存在重名或加密条目")
            seen.add(folded)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("归档含有链接或特殊文件")
            if item.file_size > max(item.compress_size, 1) * 1000:
                raise ValueError("归档条目的压缩比超过上限")
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as reader, target.open("xb") as out:
                    shutil.copyfileobj(reader, out, length=1024 * 1024)
    if not indexed:
        return None
    index = json.loads((destination / "index.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in index["files"]}
    actual = {entry["path"]: entry for entry in entries(destination)}
    if len(expected) != len(index["files"]) or actual != expected:
        raise ValueError("归档内容与索引不符")
    return index
