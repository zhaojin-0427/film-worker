"""Checks and annotation overlays shared with the original production workflow."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .media import frame_path, write_json


def validate_meta(path, count):
    if not path.is_file():
        raise ValueError("Codex 尚未生成视频内容信息 meta.jsonl")
    next_number = 1
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError("meta.jsonl 的每行须为一个镜头对象")
        end = entry.get("end_frame")
        description = entry.get("description")
        if (type(entry.get("start_frame")) is not int or entry["start_frame"] != next_number
                or type(end) is not int or end < next_number or end > count
                or not isinstance(description, str) or not description.strip()):
            raise ValueError("meta.jsonl 需要按帧号连续描述各镜头中实际发生的内容")
        next_number = end + 1
    if next_number != count + 1:
        raise ValueError("meta.jsonl 尚未覆盖全片")


def write_feedback(stage, previous, version, frames):
    entries = []
    for frame in frames:
        entry = dict(frame)
        entries.append(entry)
        regions = frame.get("regions", [])
        if not regions:
            continue
        filename = f"{frame['number']:04d}.jpg"
        relative = Path("feedback-images") / filename
        (stage / relative.parent).mkdir(exist_ok=True)
        with Image.open(previous / "pairs" / filename) as pair:
            pair.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(pair)
            font = ImageFont.load_default(size=18)
            for index, region in enumerate(regions, 1):
                x, y = region["x"], region["y"]
                bounds = (round(x * (pair.width - 1)), round(y * (pair.height - 1)),
                          round((x + region["w"]) * (pair.width - 1)),
                          round((y + region["h"]) * (pair.height - 1)))
                draw.ellipse(bounds, outline="#26160b", width=5)
                draw.ellipse(bounds, outline="#ffb429", width=3)
                label = str(index)
                label_width = round(draw.textlength(label, font=font)) + 8
                left = max(0, min(bounds[0], pair.width - label_width))
                top = max(0, min(bounds[1] - 22, pair.height - 22))
                draw.rectangle((left, top, left + label_width, top + 21), fill="#26160b")
                draw.text((left + 4, top + 2), label, font=font, fill="white", anchor="lt")
            pair.save(stage / relative, quality=94, subsampling=0)
        entry["marked_comparison"] = str(relative)
    write_json(stage / "feedback.json", {"version": version, "frames": entries})


def verify_rendered_frame(path, size):
    with Image.open(path) as frame:
        if frame.format != "PNG" or frame.size != size:
            raise ValueError("PNG 尺寸不符")
        frame.verify()


def check_rendered_frames(folder, size, count, *, complete):
    """Only complete PNGs of the requested size can be resumed or published."""
    for number in range(1, count + 1):
        path = frame_path(folder, number)
        if not path.exists() and not complete:
            continue
        try:
            verify_rendered_frame(path, size)
        except (OSError, ValueError, SyntaxError) as exc:
            if complete:
                raise ValueError(f"第 {number} 帧缺失、损坏或尺寸与 {size[0]}×{size[1]} 不符") from exc
            # Atomic render checkpoints can be safely rebuilt without touching published images.
            path.unlink()
    if complete and sum(1 for _ in folder.glob("[0-9]*.png")) != count:
        raise ValueError("渲染目录的编号帧数与原片时间线不一致")
