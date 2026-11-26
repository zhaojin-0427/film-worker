"""Translate a server work order into local media production."""
import contextlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import threading
import subprocess
import sys
import uuid
from fractions import Fraction
from pathlib import Path

from . import blender_author, bundles, draft_review, media, model, model_recovery, quality, shot_draft, visual_review
from .actor_guard import ACTOR_GEOMETRY
from .engineering_checks import EngineeringValidationError
from .progress import Progress
from .production_helpers import check_rendered_frames, validate_meta, write_feedback
from .production_palette import prompt_table
from .protocol import digest
from .settings import REVIEW_JPEG, atomic_json, object_hash
from .processes import ProcessTree
from .state import slot

HERE = Path(__file__).resolve().parent
BODY_MODEL = HERE / "assets/基础模型.blend"
SKILL = HERE / "skills/movie-shot-workflow"
VIDEO_SUFFIXES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
                  "video/webm": ".webm", "video/x-msvideo": ".avi"}


def size(value):
    return value["width"], value["height"]


def dimensions(value):
    return dict(zip(("width", "height"), value, strict=True))


def to_wire(timeline, compatibility):
    endpoints = timeline["pts"] + [timeline["end"]]
    base = Fraction(timeline["time_base"])
    return dict(schema_version=1, source_sha256=timeline["source_sha256"],
                decoder_id=compatibility["decoder_id"], source_size=dimensions(timeline["source_size"]),
                display_size=dimensions(timeline["display_size"]), time_base=f"{base.numerator}/{base.denominator}",
                end_pts=timeline["end"], frame_count=len(timeline["pts"]), average_fps=timeline["fps"],
                frames=[dict(number=i, pts=a, duration_pts=b-a) for i, (a, b) in enumerate(
                    zip(endpoints, endpoints[1:]), 1)], variable_rate=timeline["variable_rate"],
                audio_stream_index=timeline["audio_index"], audio_offset=timeline["audio_offset"])


def review_dimensions(timeline, profile):
    w, h = timeline["width"], timeline["height"]
    limit = profile["review_landscape_limit" if w >= h else "review_portrait_limit"]
    scale = min(Fraction(1), Fraction(limit["width"], w), Fraction(limit["height"], h))
    return max(1, round(w * scale)), max(1, round(h * scale))


class Production:
    def __init__(self, directory, request):
        self.root = directory
        self.request = request
        self.order = request["assignment"]["input_manifest"]
        self.kind = self.order["kind"]
        self.review_storage = self.order.get("review_storage")
        self.profile = request.get("production") or self.order.get("profile") or {}
        self.env = request["environment"].copy()
        self.tools = request["tools"]
        self.sources = {key: Path(path) for key, path in request["paths"].items()}
        self.local_version = self.sources.pop("local_version", None)
        if self.local_version is not None:
            self.sources.update(scene_bundle=self.local_version / "out/scene.zip",
                                review_bundle=self.local_version / "out/review.zip")
        self.stage = directory / "scene"
        self.stage.mkdir(exist_ok=True)
        self.output = directory / "out"
        self.output.mkdir(exist_ok=True)
        self.progress = Progress(directory)
        self.count = 0
        settings = request.get("review_settings", {})
        self.review_clarity = settings.get("review_clarity")
        self.dual_role = settings.get("dual_role_review", False)
        self.recovery_at_start = self.model_checkpoint()

    def blender_command(self, *arguments):
        return [sys.executable, str(HERE / "blender_author.py"), self.tools["blender"], *arguments]

    def run_tool(self, name, command, *, stdin=None, env=None, frames=None, stage=None, engineering=False, on_frames=None):
        logpath = self.root / (name + ".log")
        with contextlib.ExitStack() as stack:
            log = stack.enter_context(logpath.open("ab"))
            offset = log.tell()
            prompt = stack.enter_context(stdin.open("rb")) if stdin else subprocess.DEVNULL
            if on_frames:
                tree = ProcessTree(command, cwd=self.stage, env=env or os.environ, log=log, stdin=prompt)
                stack.callback(tree.close, wait_for_children=True)
                child = tree.child
            else:
                child = subprocess.Popen(command, cwd=self.stage, env=env, stdin=prompt, stdout=log,
                                         stderr=subprocess.STDOUT,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while child.poll() is None:
                if on_frames:
                    on_frames()
                if frames is not None and not on_frames:
                    self.progress(stage, sum(1 for p in frames.glob("*.png") if p.stem.isdigit()), self.count)
                try:
                    child.wait(timeout=.25 if on_frames else 2)
                except subprocess.TimeoutExpired:
                    pass
            if on_frames and child.returncode == 0:
                on_frames()
        if child.returncode:
            if engineering and child.returncode == 1:
                with logpath.open("rb") as log:
                    log.seek(max(offset, logpath.stat().st_size - 4096))
                    output = log.read().decode("utf-8", "replace")
                    report = re.search(r"(?m)^(?:engineering_checks\.)?EngineeringValidationError: (.+)$",
                                       output)
                if report:
                    raise EngineeringValidationError(report[1])
                if name == "compile-draft" and re.search(
                    r"(?m)^AttributeError: Vector [^\r\n]*numpy\.ndarray[^\r\n]*\r?$", output
                ):
                    raise EngineeringValidationError(
                        "补建脚本混用了 Blender Vector 和 NumPy 数组。请查看 compile-draft.log 的出错行，"
                        "将 NumPy 标量转为 float，向量坐标转为 mathutils.Vector，再重跑出错分支。"
                    )
            raise RuntimeError(f"{name} 退出码 {child.returncode}，详见本机日志")

    def run(self):
        if not quality.may_author(self.profile, self.kind):
            raise ValueError("正式批次要求人工白模，不领取 Codex 建模或返修")
        if "export_quality" in self.order and (
                self.kind != "finalize" or self.order["export_quality"] not in ("1080p", "4k")):
            raise ValueError("成片质量仅适用于出片工单，可选 1080p 或 4k")
        artifacts = {item["role"]: item for item in self.order["artifacts"]}
        self.progress("prepare_source")
        media_type = artifacts["source_video"]["media_type"]
        original_suffix = VIDEO_SUFFIXES.get(media_type) or mimetypes.guess_extension(media_type) or ".video"
        source = self.root / ("source" + original_suffix)
        source_sha = artifacts["source_video"].get("sha256") or digest(self.sources["source_video"])
        if not source.exists() or digest(source) != source_sha:
            temporary = source.with_suffix(".partial")
            shutil.copyfile(self.sources["source_video"], temporary)
            if digest(temporary) != source_sha:
                raise ValueError("源片校验失败")
            temporary.replace(source)
        width = (size(media.read_json(self.sources["version_manifest"])["final_size"])[0]
                 if self.kind != "prepare" else self.profile.get("final_width", 3840))
        timeline = media.prepare_source(self.root, source, width)
        self.timeline = timeline
        self.count = len(timeline["pts"])
        self.final_size = timeline["width"], timeline["height"]
        self.review_size = review_dimensions(timeline, self.profile)
        self.frames = self.root / "intake/frames"
        wire = to_wire(timeline, self.profile["compatibility"])
        timeline["time_base"] = wire["time_base"]
        media.write_json(self.root / "intake/timeline.json", timeline)
        if self.kind == "prepare":
            self.timeline_file = self.output / "timeline.json"
            media.write_json(self.timeline_file, wire)
            self.previous = None
        else:
            self.timeline_file = self.sources["source_timeline"]
            frozen = media.read_json(self.timeline_file)
            if any(frozen.get(k) != v for k, v in wire.items() if k != "decoder_id"):
                raise ValueError("本机重建的源片时间线与冻结版本不同")
            self.load_previous()
        self.timeline_sha = digest(self.timeline_file)
        if self.kind == "finalize":
            return self.finalize(source)
        return self.create_review(source)

    def load_previous(self):
        base = media.read_json(self.sources["version_manifest"])
        if (base["job_id"], base["version"], base["frame_count"], base["source_sha256"],
                base["timeline_sha256"]) != (
                self.order["job_id"], self.order["base_version"], self.count, self.timeline["source_sha256"],
                digest(self.sources["source_timeline"])):
            raise ValueError("基版的作业、版号、原片或时间线不符")
        if quality.policy(self.profile) == "human_required" and base.get("production_method") != "human":
            raise ValueError("正式出片缺少已审核的人工作品来源")
        if base.get("review_storage") != self.review_storage:
            raise ValueError("基版审片存储模式或绑定实例不符")
        self.previous = base
        self.review_size = size(base["review_size"])
        if size(base["final_size"]) != self.final_size:
            raise ValueError("基版交付画幅不符")
        self.prior_scene = self.root / "prior-scene"
        self.prior_review = self.root / "prior-review"
        self.prior_white = self.local_version / "local-review" if self.local_version is not None else self.prior_review
        for target in (self.prior_scene, self.prior_review):
            if target.exists():
                shutil.rmtree(target)
        scene = bundles.extract(self.sources["scene_bundle"], self.prior_scene, indexed=True)
        review = bundles.extract(self.sources["review_bundle"], self.prior_review, indexed=True)
        self.scene_file = self.prior_scene / "scene.blend"
        if (digest(self.scene_file), scene["scene_sha256"], review["scene_sha256"],
                digest(self.sources["scene_bundle"]), digest(self.sources["review_bundle"]),
                digest(self.sources["shot_metadata"])) != (
                base["scene_sha256"], base["scene_sha256"], base["scene_sha256"], base["scene_bundle_sha256"],
                base["review_bundle_sha256"], base["shot_metadata_sha256"]):
            raise ValueError("基版工程或审片文件已变更")
        if any(review.get(k) != base.get(k) for k in (
                "job_id", "source_sha256", "timeline_sha256", "review_size", "final_size", "frames", "review_storage")):
            raise ValueError("基版审核索引不匹配")
        if [f["number"] for f in base["frames"]] != list(range(1, self.count + 1)):
            raise ValueError("审核帧索引不连续")
        for frame, pts in zip(base["frames"], self.timeline["pts"], strict=True):
            if frame["pts"] != pts:
                raise ValueError("审核索引与 PTS 不符")
            for folder, role in ((self.prior_review, "pair_jpeg_path"), (self.prior_white, "white_png_path")):
                file = folder / bundles.safe_path(frame[role])
                if not file.is_file():
                    raise ValueError("审核帧图片缺失")

    def approval(self):
        approvals = media.read_json(self.sources["approval_manifest"])
        base = self.previous
        required = dict(job_id=self.order["job_id"], base_version=base["version"],
                        base_manifest_sha256=digest(self.sources["version_manifest"]),
                        source_sha256=base["source_sha256"], timeline_sha256=base["timeline_sha256"],
                        scene_sha256=base["scene_sha256"], frame_count=self.count,
                        scope="all_frames" if self.kind == "finalize" else "approved_subset")
        if any(approvals.get(k) != v for k, v in required.items()) or not approvals.get("original_annotator_id"):
            raise ValueError("审批清单不是当前基版的冻结审批")
        frames = self.verify_items(approvals["frames"])
        if self.kind == "finalize" and len(frames) != self.count:
            raise ValueError("审批清单未覆盖全片")
        return approvals["original_annotator_id"], frames

    def verify_items(self, items):
        frames = {frame["number"]: frame for frame in self.previous["frames"]}
        result = {}
        for item in items:
            n = item["number"]
            if n in result or n not in frames or item["review_signature"] != frames[n]["review_signature"]:
                raise ValueError("标注或审批记录与基版逐帧签名不符")
            result[n] = frames[n]
        return result

    def create_review(self, source):
        if self.previous:
            annotator, approved = self.approval()
            feedback = media.read_json(self.sources["feedback_manifest"])
            conditions = dict(job_id=self.order["job_id"], base_version=self.previous["version"],
                              original_annotator_id=annotator, timeline_sha256=self.timeline_sha,
                              base_manifest_sha256=digest(self.sources["version_manifest"]),
                              review_size=self.previous["review_size"])
            marked = self.verify_items(feedback["frames"])
            if not marked or marked.keys() & approved.keys() or any(feedback.get(k) != v for k,v in conditions.items()):
                raise ValueError("返修单与基版或原标注员不一致")
            # Feedback helper expects chronological JPEG names.
            images = self.root / "feedback-base/pairs"
            images.mkdir(parents=True, exist_ok=True)
            for n, frame in marked.items():
                shutil.copyfile(self.prior_review / bundles.safe_path(frame["pair_jpeg_path"]), images / f"{n:04d}.jpg")
            write_feedback(self.stage, images.parent, self.previous["version"], feedback["frames"])
        scene = self.stage / "scene.blend"
        meta = self.stage / "meta.jsonl"
        prepared = self.root / "prepared.json"
        expected = self.request["assignment"]["input_manifest_sha256"]
        saved = media.read_json(prepared) if prepared.is_file() else {}
        repair_state = self.root / "visual-repair.json"
        repair = model_recovery.read_state(repair_state) if self.dual_role else {}
        if repair.get("input") != expected:
            repair = dict(input=expected, round=0)
        hashes = {"input": expected, "scene": digest(scene) if scene.is_file() else "",
                  "meta": digest(meta) if meta.is_file() else ""}
        if saved and (all(saved.get(k) == v for k, v in hashes.items())
                      or (saved.get("input") == expected and "issues" in repair)):
            self.env.update(saved["environment"])
        else:
            recovery = self.recovery_at_start
            if self.previous and not recovery:
                shutil.copyfile(self.scene_file, scene)
                shutil.copyfile(self.sources["shot_metadata"], meta)
                if (self.prior_scene / ACTOR_GEOMETRY).is_file():
                    shutil.copyfile(self.prior_scene / ACTOR_GEOMETRY, self.stage / ACTOR_GEOMETRY)
            attempted = self.stage / ".engineering-repair-attempted"
            repairing = attempted.is_file() and recovery.get("phase") in {"codex-repair", "codex-self-review"}
            try:
                self.author_and_validate(source, scene, meta, repair=attempted.read_text(encoding="utf-8") if repairing else None)
            except EngineeringValidationError as exc:
                if attempted.exists():
                    raise
                attempted.write_text(str(exc) + "\n", encoding="utf-8")
                self.author_and_validate(source, scene, meta, repair=str(exc))
            atomic_json(prepared, dict(input=expected, scene=digest(scene), meta=digest(meta), environment=self.env))
        if not self.dual_role:
            return self.package_review(scene, meta)
        while True:
            if "issues" in repair:
                self.repair_visual(source, scene, meta, visual_review.RevisionNeeded(repair["issues"]), object_hash(repair))
                atomic_json(prepared, dict(input=expected, scene=digest(scene), meta=digest(meta), environment=self.env))
                repair = dict(input=expected, round=repair["round"])
                atomic_json(repair_state, repair)
            try:
                self.preview_review(scene, meta)
                return self.package_review(scene, meta)
            except visual_review.RevisionNeeded as exc:
                if repair["round"] >= visual_review.MAX_REPAIRS:
                    raise visual_review.VisualReviewError(f"双角色审核返修达上限：{exc}") from None
                repair = dict(input=expected, round=repair["round"] + 1, issues=exc.issues)
                atomic_json(repair_state, repair)

    def package_review(self, scene, meta):
        raw_meta = meta.read_text(encoding="utf-8")
        metadata = self.output / "meta.jsonl"
        with metadata.open("w", encoding="utf-8") as f:
            for line in raw_meta.splitlines():
                if line.strip():
                    shot = json.loads(line)
                    f.write(json.dumps({k: shot[k] for k in ("start_frame","end_frame","description")}, ensure_ascii=False)+"\n")
        scene_sha = digest(scene)
        scene_bundle = self.output / "scene.zip"
        bundles.build(self.stage_bundle(scene), dict(schema_version=1, scene_path="scene.blend", scene_sha256=scene_sha),
                      scene_bundle)
        review_root = self.root / "review-bundle"
        white_root = self.root / "local-review" if self.review_storage else review_root
        (review_root / "pairs").mkdir(parents=True, exist_ok=True)
        (white_root / "white").mkdir(parents=True, exist_ok=True)
        frames = []
        prior = {row["number"]: row for row in self.previous["frames"]} if self.previous else {}
        def save_pair(i, rendered):
            png = media.frame_path(rendered, i)
            white = media.rgb(png)
            pair = media.comparison(self.frames, white, i)
            signature = media.signature(pair, self.timeline, i)
            jpg_name, png_name = f"pairs/{i:04d}.jpg", f"white/{i:04d}.png"
            target = review_root / jpg_name
            if i in prior and prior[i]["review_signature"] == signature:
                shutil.copyfile(self.prior_review / bundles.safe_path(prior[i]["pair_jpeg_path"]), target)
            else:
                if self.review_clarity:
                    quality_value, subsampling = REVIEW_JPEG[self.review_clarity]
                    pair.save(target, quality=quality_value, subsampling=subsampling, optimize=True)
                else:
                    pair.save(target, quality=94, subsampling=0)
            shutil.copyfile(png, white_root / png_name)
            frames.append(dict(number=i, pts=self.timeline["pts"][i - 1], review_signature=signature,
                               pair_jpeg_path=jpg_name, white_png_path=png_name))
            return target

        approval = None
        if self.dual_role:
            required = visual_review.review_frames(meta, self.count, Fraction(self.timeline["fps"]), preview=False)
            review = visual_review.VisualReview(self, scene, required, preview=False)
            wanted = set(required)

            def on_frames(folder):
                review.poll()
                while len(frames) < self.count and media.frame_path(folder, len(frames) + 1).is_file():
                    i = len(frames) + 1
                    target = save_pair(i, folder)
                    if i in wanted:
                        review.add(i, target)
                        review.poll()
                self.progress("render_review_" + self.env["render_backend"], len(frames), self.count,
                              render_size=self.review_size)

            try:
                self.render(scene, self.review_size, "review", on_frames=on_frames)
                self.progress("codex_auditor")
                approval = review.finish()
            finally:
                review.close()
            if digest(scene) != scene_sha or approval["scene_sha256"] != scene_sha:
                raise visual_review.VisualReviewError("审核期间工程已变化")
        else:
            rendered = self.render(scene, self.review_size, "review")
            for i in range(1, self.count + 1):
                self.progress("compare_review", i, self.count)
                save_pair(i, rendered)
        common = dict(schema_version=1, job_id=self.order["job_id"], source_sha256=self.timeline["source_sha256"],
                      timeline_sha256=self.timeline_sha, scene_sha256=scene_sha,
                      review_size=dimensions(self.review_size), final_size=dimensions(self.final_size), frames=frames)
        if "profile_hash" in self.profile:
            common["profile_hash"] = self.profile["profile_hash"]
        if self.review_storage:
            common["review_storage"] = self.review_storage
        self.progress("package_review")
        review_bundle = self.output / "review.zip"
        bundles.build(review_root, dict(common, target_version=self.order["target_version"]), review_bundle)
        report = self.output / "production.json"
        records = self.stage / "制作记录.md"
        details = records.read_text(encoding="utf-8")[:30000] if records.is_file() else ""
        media.write_json(report, dict(kind=self.kind, frame_count=self.count, environment=self.env, notes=details,
                                      **({"visual_review": approval} if approval else {})))
        manifest = self.output / "version.json"
        media.write_json(manifest, dict(common, version=self.order["target_version"], frame_count=self.count,
                                        production_method="codex_trial", scene_bundle_sha256=digest(scene_bundle), review_bundle_sha256=digest(review_bundle),
                                        shot_metadata_sha256=digest(metadata), production_report_sha256=digest(report),
                                        input_manifest_sha256=self.request["assignment"]["input_manifest_sha256"],
                                        environment=self.env))
        outputs = dict(scene_bundle=scene_bundle, shot_metadata=metadata, review_bundle=review_bundle,
                       version_manifest=manifest, production_report=report)
        if self.review_storage:
            del outputs["scene_bundle"]
        if self.kind == "prepare":
            outputs["source_timeline"] = self.timeline_file
        result = dict(outputs={k: str(v) for k,v in outputs.items()}, environment=self.env)
        if approval:
            result["automatic_review"] = dict(approval, outputs_sha256={k: digest(v) for k, v in outputs.items()})
        return result

    def stage_bundle(self, scene):
        portable = self.root / "packed-scene"
        portable.mkdir(exist_ok=True)
        shutil.copyfile(scene, portable / "scene.blend")
        if (self.stage / ACTOR_GEOMETRY).is_file():
            shutil.copyfile(self.stage / ACTOR_GEOMETRY, portable / ACTOR_GEOMETRY)
        return portable

    def author_and_validate(self, source, scene, meta, *, repair=None):
        self.progress("waiting_for_codex")
        resources = Path(self.request["resources"])
        with slot(resources, "codex", self.request["model_slots"]):
            if repair is None:
                self.author(source)
            else:
                self.author(source, repair=repair)
        self.validate_scene(scene, meta)

    def validate_scene(self, scene, meta):
        if not scene.is_file():
            raise EngineeringValidationError("缺少可编辑的 Blender 工程 scene.blend")
        try:
            validate_meta(meta, self.count)
        except ValueError as exc:
            raise EngineeringValidationError(str(exc)) from exc
        self.progress("validate_scene")
        with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
            self.run_tool("blender-prepare", self.blender_command("-b", "--disable-autoexec", str(scene),
                          "--python-exit-code", "1", "-P", str(HERE / "prepare_render.py")),
                          env=dict(os.environ, MOVIE_JOB_TIMELINE=str(self.root / "intake/timeline.json"),
                                   MOVIE_BUILTIN_RULES="1"), engineering=True)

    def preview_review(self, scene, meta):
        needed = visual_review.review_frames(meta, self.count, Fraction(self.timeline["fps"]), preview=True)
        review = visual_review.VisualReview(self, scene, needed, preview=True)
        folder = review.directory / "preview"
        backend = self.env["render_backend"]
        scale = min(Fraction(1), Fraction(480, max(self.review_size)))
        extent = tuple(media.even(axis * scale) for axis in self.review_size)
        args = self.blender_command(
            "-b", "--disable-autoexec", str(scene), "--python-exit-code", "1", "-P", str(HERE / "render_frames.py"),
            "--", str(folder), backend, *map(str, extent), str(self.request["cpu_threads"]), "--draft",
            "--cache-dir", str(self.root / ".gpu-cache"), "--frames", *map(str, needed),
        )
        cursor = 0

        def on_frames():
            nonlocal cursor
            review.poll()
            while cursor < len(needed) and (path := media.frame_path(folder, needed[cursor])).is_file():
                number = needed[cursor]
                image = media.comparison(self.frames, media.rgb(path), number)
                pair = review.directory / f"preview-{number:05d}.jpg"
                image.save(pair, quality=90, optimize=True)
                review.add(number, pair)
                cursor += 1
                review.poll()

        self.progress("preview_visual_review")
        try:
            with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
                self.run_tool("preview-visual-review", args, on_frames=on_frames)
            self.progress("preview_auditor")
            review.finish()
        finally:
            review.close()

    def run_auditor(self, prompt, directory, fingerprint, cancelled, deadline, *, preview):
        name = visual_review.AUDITOR
        input_file = directory / "prompt.txt"
        input_file.write_text(prompt, encoding="utf-8")
        receipt = directory / "conclusion.json"
        receipt.unlink(missing_ok=True)
        room = directory.parent
        base = model.options(self.tools["codex"], self.profile, room, sandbox="read-only")

        def command(session):
            output = ["-o", str(receipt)]
            return [*base, "resume", "--skip-git-repo-check", *output, session, "-"] if session else [*base, *output, "-"]

        with slot(Path(self.request["resources"]), "codex", self.request["model_slots"], cancelled):
            recovery = model_recovery.CodexRecovery(
                self.root, name, self.request["assignment"].get("input_manifest_sha256"),
                lease_owner=self.request.get("codex_lease_owner"), unattended=True, journal="auditor", context_hash=fingerprint,
            )
            recovery.run(command, cwd=room, env=model.command_environment(os.environ), prompt=input_file,
                         resume_prompt=input_file, log_path=directory / "codex.log", cancelled=cancelled, deadline=deadline,
                         initial_session=recovery.data["sessions"].get(name))
        return receipt.read_text(encoding="utf-8")

    def repair_visual(self, source, scene, meta, revision, context_hash):
        self.progress("codex_visual_repair")
        prompt = self.author_prompt(source) + (
            "\n独立审核结论如下。修正对应帧段，保留其余已对齐内容，保存更新后的工程和制作记录。"
            "首制应同步更新 shot-plan.json 与补建脚本。执行端负责重新渲染和送给独立审核员。\n"
            + json.dumps(revision.issues, ensure_ascii=False)
        )
        saved = model_recovery.checkpoint(self.root)
        session = next((saved.get("sessions", {}).get(name) for name in ("codex-visual-repair", "codex-repair", "codex-resume", "codex")
                        if saved.get("sessions", {}).get(name)), None)
        if not (saved.get("phase") == "codex-visual-repair" and saved.get("context_hash") == context_hash
                and saved.get("state") == "completed"):
            with slot(Path(self.request["resources"]), "codex", self.request["model_slots"]):
                self.run_model(prompt, "codex-visual-repair", resume=session, context_hash=context_hash)
        if self.kind == "prepare" and not shot_draft.current(self.stage):
            compile_cmd, _ = self.draft_commands(source)
            with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
                self.run_tool("compile-draft", compile_cmd, engineering=True)
        self.validate_scene(scene, meta)

    def draft_commands(self, source):
        compile_cmd = shot_draft.compile_command(self.blender_command(), self.stage)
        review_cmd = [sys.executable, str(HERE / "draft_review.py"), str(self.stage), str(source),
                      self.tools["blender"], str(self.request["cpu_threads"]),
                      "--backend", self.env["render_backend"],
                      "--ffmpeg", self.tools["ffmpeg"], "--ffprobe", self.tools["ffprobe"]]
        return compile_cmd, review_cmd

    def author_prompt(self, source, repair=None):
        def quote(arguments):
            if os.name == "nt":
                return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
            return shlex.join(arguments)

        compile_cmd, review_cmd = self.draft_commands(source)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        sop = (SKILL / "references/制作SOP.md").read_text(encoding="utf-8")
        review_step = ("保存工程和制作记录后结束首制，由执行端生成全片自检对比片，再开始复查。"
                       if os.name == "nt" else f"""随后按原时间线编码全部帧的首版自检对比：
{quote(review_cmd)}
查看整条自检片。无法播放时从编码后的视频解出连续画面，逐镜复查，发现偏差就修正、重编、重新编码再检查。""")
        if self.dual_role:
            review_step = "保存工程、镜头表和制作记录后结束。执行端将试渲并交给独立审核员。"
        phase = f"""这是 Codex 内部试制首制。先核对完整原片、编号帧和 镜头参考，使用连续帧修正候选切点。
完成 shot-plan.json 后用下列命令编译出完整 scene.blend 与 meta.jsonl：
{quote(compile_cmd)}
可追加 --preview 与帧号快速试渲。{review_step}
镜头表规范如下。
{(SKILL / 'references/镜头表.md').read_text(encoding='utf-8')}
""" if self.kind == "prepare" else """这是 Codex 内部试制返修。当前目录已有基版场景、镜头信息、返修单和圈选预览。
先读 feedback-summary.json，再按需要检查 feedback.json 原文、圈选图及连续源帧。按帧号核对受影响的动作范围和相邻帧，修改 scene.blend 及需要更新的 meta.jsonl。
圈选坐标相对于整张上下对比图，上方原片、下方白模。补建网格使用包内 production_palette.apply_palette 并保留原来的人体几何及其配色属性。
"""
        technical = (f"\n工程核验失败，工作台要求做一次技术修复。实际错误：{repair}\n"
                     "仅修复此问题及直接受影响的文件。修正后保存最新工程和制作记录。") if repair else ""
        if os.name == "nt":
            frame_filter = r"select=eq(n\,0)"
            extract = quote([self.tools["ffmpeg"], "-nostdin", "-v", "error", "-i", str(source),
                             "-vf", frame_filter, "-frames:v", "1", "-update", "1", "-y",
                             str(self.stage / "检查帧-0001.png")])
            probe = quote([self.tools["ffprobe"], "-v", "error", "-show_streams", "-of", "json", str(source)])
            handoff = ("保存当前工程输入和制作记录，由执行端生成送审对比。" if self.dual_role else
                       "首制或自检修正后保存当前工程输入和制作记录，由执行端生成全片自检对比片，再逐镜复查。"
                       if self.kind == "prepare" else "返修后保存工程和制作记录，由执行端重渲并生成送审对比。")
            media_tools = f"""## Windows 看图与自检
首制、返修和自检均直接看编号 PNG、镜头参考和已有的反馈图。用包内 FFmpeg 对原片、自检片及试渲结果抽帧、缩放或拼图，使用 FFprobe 读媒体信息。
下列 PowerShell 命令按显示顺序抽原片第 1 帧。检查第 F 帧时把 n 的比较值改为 F-1，修改输出文件名。检查对比片时把输入改为现有的首版自检对比.mp4。
{extract}
{probe}
执行端使用 PyAV 和 Pillow 解码、生成参考图、编码自检片并核验时序。模型进程及其临时脚本禁止导入 av、PIL 或 Pillow。
{handoff}"""
        else:
            media_tools = f"如需从自检 MP4 读取编号帧，FFmpeg 路径为 {quote([self.tools['ffmpeg']])}。"
        script_types = """## 建模脚本的数值和 JSON
Blender 向量运算两侧使用 mathutils.Vector。来自 NumPy 的标量先转为 Python float，例如 fixed_eye + ray * float(d)。数组进入 Blender 时使用 Vector(tuple(float(x) for x in coords))。
写 shot-plan.json、meta.jsonl 或检查报告之前，将 Vector、Euler 转成 [float(x) for x in value]，NumPy 数组用 .tolist()，NumPy 标量用 .item()，帧号和索引统一用 int()。调用 json.dump 或 json.dumps 时设置 allow_nan=False，让 NaN 和 Infinity 在写入前报错。
先在 Blender 后台实际运行有数值转换的路径，包括接触求解的回退分支，检查 JSON 能重新读入且坐标和帧号类型正确。再编译和试渲受影响的代表帧。"""
        return f"""原片 {source}。编号源帧 {self.frames}。本轮只写入 {self.stage}。
使用包内人体白模 {BODY_MODEL}，以及随本提示附带的制作规则、镜头表与固定配色。制作帧从 1 到 {self.count}，
沿用原片显示时间，工程画幅 {self.final_size}。全片渲染、签名比对、送审和成片由执行端处理。
Blender 启动只能用后台前缀 {quote(self.blender_command())}，追加 -b --disable-autoexec --python-exit-code 1。
检查投影与预览也沿用此命令。Windows PowerShell 请直接使用上述带调用符 & 的命令。
所有制作和试渲统一用 Cycles。禁止在补建脚本或工程中切换到 Eevee、Workbench。
提交本轮 制作记录.md，写明切点、可见动作、核对过的帧段和需要人工判断的地方。
禁止用源帧贴图、加入外部素材、重塑甲方人体或访问历史工单和用户目录。

{phase}
{technical}
{skill}
{sop}
{media_tools}
{script_types}

## 工作台内置配色
{prompt_table()}
"""

    def model_checkpoint(self):
        checkpoint = model_recovery.checkpoint(self.root)
        return checkpoint if checkpoint.get("input_hash") == self.request["assignment"].get("input_manifest_sha256") else {}

    def run_model(self, text, name, *, resume=None, context_hash=None):
        prompt = self.root / (name + "-prompt.txt")
        prompt.write_text(text, encoding="utf-8")
        resume_prompt = self.root / (name + "-resume-prompt.txt")
        resume_prompt.write_text(
            "接续刚才中断的本工单和当前制作阶段。以本轮目录中已保存的工程、镜头表、编号 PNG、自检片和制作记录为依据，"
            "检查上一步结果，继续未完成的工作。用包内 FFmpeg 看图，保存实际制作或自检记录后结束本轮。\n",
            encoding="utf-8",
        )
        overrides = dict(self.profile)
        if self.kind == "prepare" and not overrides.get("reasoning_effort"):
            overrides["reasoning_effort"] = "medium"
        with blender_author.background_environment(self.tools["blender"]) as background:
            env = model.command_environment(background)
            base = model.options(self.tools["codex"], overrides, self.stage, sandbox="workspace-write",
                                    job_root=self.root if os.name == "nt" else None,
                                    blender_config=env.get("BLENDER_USER_CONFIG"))
            receipt = self.root / (name + "-receipt.txt")

            def command(session):
                output = ["-o", str(receipt)]
                if session:
                    return [*base, "resume", "--skip-git-repo-check", *output, session, "-"]
                return [*base, *output, "-"]

            recovery = model_recovery.CodexRecovery(
                self.root, name, self.request["assignment"].get("input_manifest_sha256"),
                lease_owner=self.request.get("codex_lease_owner"),
                unattended=self.dual_role,
                context_hash=context_hash,
            )
            actual = recovery.run(command, cwd=self.stage, env=env, prompt=prompt,
                                  resume_prompt=prompt if resume else resume_prompt,
                                  log_path=self.root / (name + ".log"), initial_session=resume)
        self.env.update(actual, model_evidence="observed" if all(actual.values()) else "unknown")

    def author(self, source, *, repair=None):
        if self.kind == "prepare" and not (self.stage / "draft-context.json").is_file():
            if not BODY_MODEL.is_file():
                raise RuntimeError("发布包缺少基础模型.blend，请恢复完整的执行端")
            self.progress("prepare_draft")
            shot_draft.prepare(self.stage, source, self.root / "intake", self.timeline, BODY_MODEL)
        prompt = self.author_prompt(source, repair)
        saved, self.recovery_at_start = self.recovery_at_start, {}
        phase = saved.get("phase")
        if self.kind == "prepare" and phase == "codex-self-review":
            if saved.get("state") == "completed":
                actual = saved.get("evidence", {})
                self.env.update(actual, model_evidence="observed" if actual and all(actual.values()) else "unknown")
            self.complete_draft(source, prompt)
            return
        name = phase if phase == "codex-resume" and repair is None else ("codex-repair" if repair else "codex")
        resume_file = self.resume_request() if repair is None else None
        if phase == name and saved.get("state") == "completed":
            actual = saved.get("evidence", {})
            self.env.update(actual, model_evidence="observed" if actual and all(actual.values()) else "unknown")
        elif resume_file:
            resume_request = media.read_json(resume_file)
            session = str(uuid.UUID(resume_request["session_id"]))
            resume_prompt = (
                "恢复本工单中断的制作会话。沿用当前镜头表、scene.blend、meta.jsonl 和已有自检片。"
                "先查看已经做过的检查并继续未完成的画面对照，发现明确偏差时只修正受影响的帧段。"
                "按实际完成情况保存制作记录.md，注明已核对的帧段、仍需人工判断的偏差，然后结束本轮。"
                "执行端会生成与最新工程对应的自检片并接手后续校验和渲染。"
                "画面提取用包内 FFmpeg，按编号查看已生成的 PNG。"
            )
            self.progress("codex_resume")
            self.run_model(resume_prompt, "codex-resume", resume=session)
            with contextlib.suppress(OSError):
                resume_file.unlink(missing_ok=True)
        else:
            self.progress({"codex": "codex_author", "codex-repair": "codex_repair", "codex-resume": "codex_resume"}[name])
            self.run_model(prompt, name)
        if self.kind == "prepare" and self.dual_role and not shot_draft.current(self.stage):
            compile_cmd, _ = self.draft_commands(source)
            with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
                self.run_tool("compile-draft", compile_cmd, engineering=True)
        elif self.kind == "prepare" and not self.dual_role:
            self.complete_draft(source, prompt)

    def resume_request(self):
        work = self.root.parent
        assignment = self.request["assignment"]
        if (work.parent.name != "work" or assignment.get("work_id") != work.name
                or str(assignment.get("lease", {}).get("attempt")) != self.root.name):
            return None
        path = work.parent.parent / "codex-resume" / work.name / f"{self.root.name}.json"
        return path if path.is_file() else None

    def complete_draft(self, source, prompt):
        if not (self.stage / "shot-plan.json").is_file():
            raise EngineeringValidationError("首版缺少 shot-plan.json")
        compile_cmd, review_cmd = self.draft_commands(source)
        resources = Path(self.request["resources"])
        record = self.stage / "制作记录.md"

        def current():
            return shot_draft.current(self.stage) and draft_review.current(self.stage)

        def recorded():
            return record.is_file() and bool(record.read_text(encoding="utf-8").strip())

        review_attempts = 2 if os.name == "nt" else 1
        review_instruction = ("现在进入首版自检。逐镜对照原片与复刻，查看切点和动作转折前后的连续帧。"
                              "核对头与胸口的朝向、四肢接触、主体位置、裁切和遮挡。发现能定位的偏差就修改工程输入，"
                              "保存更新后的镜头表、补建脚本及制作记录，由执行端重渲后再次检查。"
                              "记录实际看过的帧段、改动和仍需人工判断的偏差。"
                              if os.name == "nt" else "复查当前首版自检对比片，修正后重新编码并更新制作记录。")
        saved = self.model_checkpoint()
        if saved.get("phase") == "codex-self-review" and saved.get("state") != "completed":
            self.progress("codex_self_review")
            self.run_model(f"{prompt}\n{review_instruction}", "codex-self-review")
            review_attempts -= 1
        if current() and recorded():
            return
        for _ in range(review_attempts):
            self.progress("waiting_for_draft_blender")
            with slot(resources, "blender", self.request["render_slots"]):
                if not shot_draft.current(self.stage):
                    self.progress("draft_compile")
                    self.run_tool("compile-draft", compile_cmd, engineering=True)
                if not draft_review.current(self.stage):
                    self.progress("draft_review")
                    self.run_tool("draft-review", [*review_cmd, "--report-progress"], engineering=True)
            self.progress("codex_self_review")
            self.run_model(f"{prompt}\n{review_instruction}", "codex-self-review")
            if current() and recorded():
                return
        raise EngineeringValidationError("首版缺少本工程对应的自检对比片或制作记录")

    def render(self, scene, target, purpose, *, on_frames=None):
        self.progress("waiting_for_blender")
        with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
            return self.render_locked(scene, target, purpose, on_frames=on_frames)

    def render_locked(self, scene, target, purpose, *, on_frames=None):
        backend = self.env["render_backend"]
        options = ["cpu"] if backend == "cpu" else [backend]
        scene_sha = digest(scene)
        for option in options:
            frames = self.root / f"{purpose}-{option}"
            recorded = frames / "checkpoint.json"
            tag = dict(scene_sha256=scene_sha, dimensions=list(target),
                       renderer=self.tools["blender"], blender=self.env.get("compatibility", {}).get("blender"),
                       count=self.count, backend=option)
            if frames.exists() and (not recorded.exists() or media.read_json(recorded) != tag):
                shutil.rmtree(frames)
            atomic_json(recorded, tag)
            check_rendered_frames(frames, target, self.count, complete=False)
            args = self.blender_command("-b", "--disable-autoexec", str(scene), "--python-exit-code", "1", "-P",
                    str(HERE / "render_frames.py"), "--", str(frames), option, *map(str,target),
                    str(self.request["cpu_threads"]))
            self.progress(f"render_{purpose}_{option}", 0, self.count, render_size=target)
            try:
                self.run_tool(f"blender-{purpose}-{option}", args, frames=frames, stage=f"render_{purpose}_{option}",
                              on_frames=(lambda: on_frames(frames)) if on_frames else None)
                check_rendered_frames(frames, target, self.count, complete=True)
                self.env["render_backend"] = option
                return frames
            except (visual_review.VisualReviewError, model_recovery.RecoveryTimeout):
                raise
            except (ValueError, RuntimeError) as exc:
                if option != "cpu":
                    raise RuntimeError(f"{option.upper()} GPU 渲染失败，请查看对应 Blender 日志") from exc
                raise

    def finalize(self, source):
        selected = self.order.get("export_quality")
        if selected == "4k" and self.final_size[0] != 3840:
            raise ValueError("4K 出片需要宽 3840 的制作档案")
        output_size = self.final_size
        if selected == "1080p":
            output_size = tuple(media.even(axis) for axis in media.review_size(self.timeline))
        _, approved = self.approval()
        self.progress("verify_review", 0, self.count)
        for n, frame in approved.items():
            white = media.rgb(self.prior_white / bundles.safe_path(frame["white_png_path"]))
            pair = media.comparison(self.frames, white, n)
            if white.size != self.review_size or media.signature(pair, self.timeline, n) != frame["review_signature"]:
                raise ValueError(f"第 {n} 帧审核像素签名核验失败")
            self.progress("verify_review", n, self.count)
        approved_native = self.review_size == output_size
        if approved_native:
            def render_white(n):
                return media.rgb(self.prior_white / bundles.safe_path(approved[n]["white_png_path"]))
        else:
            rendered = self.render(self.scene_file, output_size, "final")
            def render_white(n):
                return media.rgb(media.frame_path(rendered, n))
        delivery = self.root / "delivery"
        for folder in ("target", "white_model", "comparison", "model_3d", "meta"):
            (delivery / folder).mkdir(parents=True, exist_ok=True)
        original = delivery / "target" / source.name
        shutil.copyfile(source, original)
        shutil.copyfile(self.scene_file, delivery / "model_3d/scene.blend")
        shutil.copyfile(self.sources["shot_metadata"], delivery / "meta/scene.jsonl")
        white_path, compare_path = delivery / "white_model/white.mp4", delivery / "comparison/comparison.mp4"
        compare_size = output_size[0], output_size[1]*2
        for name, video, extent, read in (
                ("white", white_path, output_size, render_white),
                ("comparison", compare_path, compare_size, lambda n: media.comparison(self.frames, render_white(n), n))):
            self.progress("encode_" + name)
            silent = self.root / f"{name}-silent.mp4"
            silent.unlink(missing_ok=True)
            video.unlink(missing_ok=True)
            media.encode_images(silent, self.timeline, extent, read)
            media.add_audio(silent, video, self.timeline, source)
            media.validate_encoded(video, self.timeline, extent)
        self.progress("verify_technical")
        quality.media_report(delivery / "meta/video-technical.jsonl", self.order.get("sample_id", self.order["job_id"]),
                             [("source_video", original), ("white_video", white_path), ("comparison_video", compare_path)],
                             formal=quality.policy(self.profile) == "human_required")
        approval = None
        if self.dual_role and quality.policy(self.profile) != "human_required":
            self.progress("codex_auditor")
            required = visual_review.review_frames(self.sources["shot_metadata"], self.count,
                                                   Fraction(self.timeline["fps"]), preview=False)
            review = visual_review.VisualReview(
                self, self.scene_file, required, preview=False,
                additional="当前审核已编码的成片，工程已经过服务端批准。如发现明显异常，返回 revise 并给出帧段。",
            )
            wanted = set(required)
            try:
                with media.av.open(str(compare_path)) as container:
                    for number, frame in enumerate(container.decode(video=0), 1):
                        if number in wanted:
                            target = review.directory / f"encoded-{number:05d}.jpg"
                            frame.to_image().save(target, quality=94, subsampling=0, optimize=True)
                            review.add(number, target)
                            review.poll()
                        if number >= required[-1]:
                            break
                approval = review.finish()
            finally:
                review.close()
        index = dict(schema_version=1, job_id=self.order["job_id"], version=self.order["target_version"],
                     version_manifest_sha256=digest(self.sources["version_manifest"]),
                     approval_manifest_sha256=digest(self.sources["approval_manifest"]),
                     timeline_sha256=self.timeline_sha, final_size=dimensions(output_size))
        archive = self.output / "delivery.zip"
        self.progress("package_delivery")
        bundles.build(delivery, index, archive)
        self.env.update({k: self.previous["environment"].get(k) for k in ("codex_model","codex_provider","reasoning_effort")},
                        model_evidence="inherited")
        verification = {k: index[k] for k in ("version_manifest_sha256", "approval_manifest_sha256", "timeline_sha256")}
        verification.update(scene_sha256=digest(self.scene_file), frame_count=self.count, verified_review_frames=self.count,
                            white_frame_count=self.count, comparison_frame_count=self.count, pts_verified=True,
                            audio_verified=True, white_size=dimensions(output_size), comparison_size=dimensions(compare_size),
                            render_source="approved_native_white" if approved_native else "locked_scene_native_render")
        outputs = dict(white_video=str(white_path), comparison_video=str(compare_path), delivery_bundle=str(archive))
        result = dict(environment=self.env, final_verification=verification, outputs=outputs)
        if approval:
            result["automatic_review"] = dict(approval, outputs_sha256={k: digest(Path(v)) for k, v in outputs.items()})
        return result


def run_child(root):
    if os.read(sys.stdin.fileno(), 1) != b"1":
        return 1

    def parent_watch():
        os.read(sys.stdin.fileno(), 1)
        if os.name == "nt":
            os._exit(1)
        os.killpg(os.getpgrp(), signal.SIGTERM)

    threading.Thread(target=parent_watch, daemon=True).start()
    request = media.read_json(root / "request.json")
    # Existing media routines use executable names on PATH. Their directories are pinned in the request.
    for tool in ("ffmpeg", "ffprobe"):
        os.environ["FILM_WORKER_" + tool.upper()] = request["tools"][tool]
    try:
        result = Production(root, request).run()
    except model_recovery.RecoveryTimeout as exc:
        print(str(exc), file=sys.stderr)
        atomic_json(root / "error.json", {"code": "model_unavailable", "message": str(exc)})
        return 1
    except visual_review.VisualReviewError as exc:
        print(str(exc), file=sys.stderr)
        atomic_json(root / "error.json", {"code": "other", "message": str(exc)[:4096], "retryable": False})
        return 1
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        atomic_json(root / "error.json", {"code": "input_invalid" if isinstance(exc, ValueError) else "render_failed",
                                         "message": type(exc).__name__})
        return 1
    atomic_json(root / "result.json", result)
    return 0
