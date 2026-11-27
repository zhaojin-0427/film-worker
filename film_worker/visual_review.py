"""Read-only visual review of the same numbered images prepared for submission."""
import hashlib
import json
import os
import shutil
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PIL import Image, ImageDraw, ImageOps

from . import model_recovery, quality
from .progress import write_progress
from .production_palette import prompt_table
from .protocol import digest
from .settings import object_hash


MAX_REPAIRS = 2
REVIEW_SECONDS = 30 * 60
AUDITOR = "codex-auditor"
CONCLUSION_VERSION = 2


class VisualReviewError(RuntimeError):
    pass


class RevisionNeeded(VisualReviewError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("；".join(f"F{row['start']}–{row['end']} {row['issue']}" for row in issues))


def review_frames(meta, count, fps, *, preview):
    step = max(1, round(fps * (2 if preview else .5)))
    frames = {1, count}
    for n in range(1, count + 1, step):
        frames.update(range(n - 1, n + 2))
    for line in meta.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        shot = json.loads(line)
        start, end = shot["start_frame"], shot["end_frame"]
        frames.update(range(start - 2, start + 3))
        frames.update(range(end - 2, end + 3))
        for quarter in (1, 2, 3):
            middle = start + (end - start) * quarter // 4
            frames.update(range(middle - 2, middle + 3))
    if not preview:
        frames.update(range(max(1, count - round(fps) + 1), count + 1))
    return sorted(n for n in frames if 1 <= n <= count)


def conclusion(raw, *, count, required, available):
    try:
        result = json.loads(raw)
        verdict, inspected, issues = (result[key] for key in ("verdict", "inspected", "issues"))
        if (verdict not in ("pass", "revise") or not isinstance(inspected, list) or not inspected
                or any(type(n) is not int or n not in available for n in inspected)
                or not isinstance(issues, list) or len(issues) > 20):
            raise ValueError
        structured = []
        for item in issues:
            start, end, source, difference, fix = (item[key] for key in ("start", "end", "source", "difference", "fix"))
            if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= count
                    or not any(start <= n <= end for n in inspected)
                    or any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 160
                           for value in (source, difference, fix))):
                raise ValueError
            structured.append(dict(start=start, end=end, source=source.strip(), difference=difference.strip(),
                                   fix=fix.strip(), issue=f"原片 {source.strip()}；偏差 {difference.strip()}；修改 {fix.strip()}"))
    except (ValueError, KeyError, TypeError):
        raise VisualReviewError("独立审核没有返回有效的可追溯结论") from None
    if verdict == "revise" and issues:
        raise RevisionNeeded(structured)
    if verdict != "pass" or issues or not set(required).issubset(inspected):
        raise VisualReviewError("独立审核证据不足，或仍有待修正的问题")
    return sorted(set(inspected)), result.get("review_token")


class VisualReview:
    def __init__(self, worker, scene, required, *, preview, additional=()):
        self.worker = worker
        self.scene_sha = digest(scene)
        self.required = required
        self.preview = preview
        self.additional = additional
        self.fingerprint = object_hash([self.scene_sha, required, preview, worker.review_clarity, additional,
                                        quality.L3_RULES, quality.REVIEW_CHECKLIST, prompt_table(), CONCLUSION_VERSION])
        self.directory = worker.root / "visual-review" / self.fingerprint
        self.directory.mkdir(parents=True, exist_ok=True)
        self.queue = self.directory / "frames.json"
        self.ready = []
        self.buffer = []
        self.sheets = []
        self.pairs = {}
        self.evidence = hashlib.sha256()
        self.review_token = secrets.token_hex(16)
        self.cancelled = Event()
        self.future = None
        self.executor = None

    def report(self):
        write_progress(self.worker.root / "visual-progress.json",
                       dict(preview=self.preview, ready=len(self.ready), total=len(self.required)))

    def add(self, number, pair_file):
        checksum = digest(pair_file)
        self.pairs[number] = (pair_file, checksum)
        self.evidence.update(f"{number}:{checksum}\n".encode())
        published = self.directory / f"F{number:05d}.jpg"
        published.unlink(missing_ok=True)
        try:
            os.link(pair_file, published)
        except OSError:
            shutil.copyfile(pair_file, published)
        self.buffer.append((number, published))
        if not self.ready or len(self.buffer) >= 8:
            self.flush()

    def flush(self, *, complete=False):
        if self.buffer:
            rows = (len(self.buffer) + 1) // 2
            sheet = Image.new("RGB", (960, rows * 568), "#151922")
            draw = ImageDraw.Draw(sheet)
            for index, (number, path) in enumerate(self.buffer):
                x, y = (index % 2) * 480, (index // 2) * 568
                with Image.open(path) as image:
                    thumb = ImageOps.contain(image, (480, 544), Image.Resampling.LANCZOS)
                    sheet.paste(thumb, (x + (480 - thumb.width) // 2, y))
                draw.text((x + 6, y + 546), f"F{number}", fill="white")
                self.ready.append(number)
            file = f"frames-{self.ready[-1]:05d}.jpg"
            temp = self.directory / ".sheet.jpg"
            sheet.save(temp, quality=90, optimize=True)
            temp.replace(self.directory / file)
            self.sheets.append(file)
            self.buffer.clear()
        state = dict(available=self.ready, sheets=self.sheets, complete=complete)
        if complete:
            state["review_token"] = self.review_token
        model_recovery.write_state(self.queue, state)
        self.report()
        if self.future is None:
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.future = self.executor.submit(self.inspect)

    def inspect(self):
        worker = self.worker
        prompt = f"""你是独立电影复刻审核员。本轮证据目录 {self.directory}，只查看此目录中 frames.json 当前列出的图。
图片每格上方为原片，下方为 Blender 复刻，底部标明源帧号。用看图工具实际打开所有已生成的图，
随着渲染推进重新读取 frames.json。 如果暂时没有新图，等待数秒再读文件，直到 complete 为 true。
需要确认细节时打开同目录的 F00001.jpg 等对应源帧全图。
禁止修改工程、反馈、图像、配置或上传任何数据。你只输出最终 JSON。
本轮是{"正式渲染前的低清试审，重点拦截可辨认的明显偏差" if self.preview else "正式送审复核，重点检查低清试审后仍存在的画面偏差"}。
{self.additional}
先检查 L3 基础外观和工作台配色，再检查镜头、姿态、接触、遮挡和动作节奏。连续帧一起看，遇到新增部件或材质改造时给出帧段并要求恢复 L3。
{quality.L3_RULES}
工作台固定配色
{prompt_table()}
发现明确需返修的问题可以立即返回 revise。每条提供帧段、原片表现 source、复刻偏差 difference 和可执行修改 fix。人物返修限定在上述动作范围，构图、裁切或透视问题指出具体镜头调整。
pass 仅在 complete 为 true 且打开并核对全部图后返回。列出实际看过的源帧，并原样带回 frames.json 的 review_token。
只返回 JSON，例如 {{"verdict":"pass","inspected":[1,2],"issues":[],"review_token":"从最后的 frames.json 读取"}} 或
{{"verdict":"revise","inspected":[2],"issues":[{{"start":2,"end":2,"source":"头朝画面右侧","difference":"白模头部正对镜头","fix":"将头部关节转向画面右侧，并核对前后相邻帧"}}]}}。
{quality.REVIEW_CHECKLIST}"""
        return worker.run_auditor(prompt, self.directory, self.fingerprint, self.cancelled,
                                  time.monotonic() + REVIEW_SECONDS, preview=self.preview)

    def poll(self):
        if self.future and self.future.done():
            # A positive answer is only valid after the complete frame list is published.
            conclusion(self.future.result(), count=self.worker.count, required=self.required, available=self.ready)
            raise VisualReviewError("独立审核在本轮渲染证据收齐前结束")

    def finish(self):
        self.flush(complete=True)
        inspected, token = conclusion(self.future.result(), count=self.worker.count, required=self.required,
                                      available=self.ready)
        if token != self.review_token:
            raise VisualReviewError("独立审核没有核对本轮完整证据")
        if any(digest(path) != checksum for path, checksum in self.pairs.values()):
            raise VisualReviewError("审核期间对比图已变化")
        return dict(scene_sha256=self.scene_sha, evidence_sha256=self.evidence.hexdigest(), inspected=inspected)

    def close(self):
        self.cancelled.set()
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
