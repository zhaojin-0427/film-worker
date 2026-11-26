"""Local checks used before activating an installed release."""
import tempfile
from pathlib import Path

from PIL import Image

from . import media
from .production import BODY_MODEL, SKILL
from .state import State


def check(root):
    import alibabacloud_oss_v2

    if not alibabacloud_oss_v2.Client:
        raise ValueError("运行环境缺少 OSS SDK")
    if root.exists() and not root.is_dir():
        raise ValueError("数据目录不可用")
    if any(not (Path(__file__).parent / "web" / asset).is_file()
           for asset in ("index.html", "panel.css", "panel.js")):
        raise ValueError("安装包的控制界面文件不完整")
    resources = (BODY_MODEL, SKILL / "SKILL.md", SKILL / "references/制作SOP.md", SKILL / "references/镜头表.md")
    if any(not file.is_file() or not file.stat().st_size for file in resources):
        raise ValueError("安装包缺少制作规则、镜头表或人体白模")
    if "libx264" not in media.av.codecs_available:
        raise ValueError("PyAV 环境无 H.264 编码器")
    import os
    if os.name == "nt":
        import win32cred
        import win32job
        assert win32cred.CRED_TYPE_GENERIC and win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    with tempfile.TemporaryDirectory(prefix="film-worker-check-") as tmp:
        store = State(Path(tmp))
        store.set("selftest", True)
        if store.setting("selftest") is not True:
            raise ValueError("SQLite 读写验证失败")
    sample = Image.frombytes("RGB", (1, 2), bytes.fromhex("000000ffffff"))
    timeline = {"source_sha256": "0"*64, "pts": [0], "time_base": "1/24", "end": 1}
    expected = "bd16c1d6572399a3c44d67a1c8445b907f6ff97a4a0d16b65a7ae046be695ca1"
    if media.signature(sample, timeline, 1) != expected:
        raise ValueError("审帧像素签名自检失败")
