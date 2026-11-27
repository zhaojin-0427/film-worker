import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from PIL import Image, JpegImagePlugin

from film_worker import bundles, media, oss_delivery, production, visual_review
from film_worker.protocol import OSS_OBJECTS, REJECTED_FRAMES_MODE, digest
from film_worker.scheduler import Scheduler, WorkFailure
from film_worker.settings import REVIEW_JPEG, atomic_json, object_hash, validate_config
from film_worker.state import State


class ReviewTests(unittest.TestCase):
    def test_repair_conclusion_requires_source_difference_and_action(self):
        issue = dict(start=2, end=3, source="头朝右", difference="复刻头朝左", fix="转动头部关节")
        result = dict(verdict="revise", inspected=[2], issues=[issue])
        with self.assertRaises(visual_review.RevisionNeeded) as repair:
            visual_review.conclusion(json.dumps(result), count=3, required=[1, 2, 3], available=[2])
        self.assertEqual(repair.exception.issues[0]["fix"], "转动头部关节")
        self.assertIn(issue["source"], str(repair.exception))
        del issue["source"]
        with self.assertRaisesRegex(visual_review.VisualReviewError, "可追溯结论"):
            visual_review.conclusion(json.dumps(result), count=3, required=[1, 2, 3], available=[2])

    def worker(self, root, clarity, local, dual=False, objects=False):
        order = {"kind": "prepare", "job_id": "1", "target_version": 1}
        if local:
            order["review_storage"] = {"mode": "node_local_v1", "instance_id": "fixture"}
        worker = production.Production(root, dict(
            assignment={"input_manifest": order, "input_manifest_sha256": "fixture",
                        **({"oss": {"protocol": OSS_OBJECTS}} if objects else {})},
            paths={}, tools={}, environment={"render_backend": "cpu"},
            review_settings={"review_clarity": clarity, "dual_role_review": dual},
        ))
        worker.timeline = {"pts": [0, 1, 2], "end": 3, "time_base": "1/24", "fps": "24", "source_sha256": "fixture"}
        worker.count = 3
        worker.review_size = worker.final_size = (32, 18)
        worker.timeline_sha = "fixture"
        worker.previous = None
        worker.frames = root / "source"
        worker.frames.mkdir()
        rendered = root / "white"
        rendered.mkdir()
        for n in range(1, 4):
            Image.new("RGB", worker.review_size, (59, 39, n)).save(worker.frames / f"{n:04d}.png")
            Image.new("RGB", worker.review_size, (59, n, 39)).save(rendered / f"{n:04d}.png")
        scene = worker.stage / "scene.blend"
        scene.write_bytes(b"fixture-scene")
        meta = worker.stage / "meta.jsonl"
        meta.write_text('{"start_frame":1,"end_frame":3,"description":"test"}\n', encoding="utf-8")
        worker.timeline_file = worker.output / "timeline.json"
        worker.timeline_file.write_text("{}", encoding="utf-8")
        return worker, rendered, scene, meta

    def test_default_and_jpeg_zip_index_for_both_storage_modes(self):
        defaults = validate_config({})
        self.assertEqual(defaults["review_clarity"], "standard")
        self.assertIs(defaults["dual_role_review"], True)
        for clarity, (_, sampling) in REVIEW_JPEG.items():
            for local in (False, True):
                with self.subTest(clarity=clarity, local=local), tempfile.TemporaryDirectory() as folder:
                    worker, rendered, scene, meta = self.worker(Path(folder), clarity, local)
                    with patch.object(worker, "render", return_value=rendered):
                        result = worker.package_review(scene, meta)
                    with zipfile.ZipFile(result["outputs"]["review_bundle"]) as archive:
                        names = archive.namelist()
                        self.assertEqual("white/0001.png" in names, not local)
                        index = json.loads(archive.read("index.json"))
                        for item in index["files"]:
                            data = archive.read(item["path"])
                            self.assertEqual(len(data), item["size_bytes"])
                            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
                        with archive.open("pairs/0001.jpg") as raw, Image.open(raw) as pair:
                            self.assertEqual(pair.size, (32, 36))
                            self.assertEqual(JpegImagePlugin.get_sampling(pair), sampling)
                    self.assertEqual("scene_bundle" in result["outputs"], not local)

    def test_equal_signature_reuses_previous_jpeg_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            worker, rendered, scene, meta = self.worker(root, "economy", True)
            worker.prior_review = root / "prior"
            old = worker.prior_review / "pairs/0001.jpg"
            old.parent.mkdir(parents=True)
            white = media.rgb(rendered / "0001.png")
            pair = media.comparison(worker.frames, white, 1)
            pair.save(old, quality=94, subsampling=0)
            worker.previous = {"frames": [{
                "number": 1, "review_signature": media.signature(pair, worker.timeline, 1),
                "pair_jpeg_path": "pairs/0001.jpg",
            }]}
            with patch.object(worker, "render", return_value=rendered):
                result = worker.package_review(scene, meta)
            with zipfile.ZipFile(result["outputs"]["review_bundle"]) as archive:
                self.assertEqual(archive.read("pairs/0001.jpg"), old.read_bytes())

    def test_revision_uploads_rejected_and_changed_frames_and_loads_the_merged_base(self):
        for local in (False, True):
            with self.subTest(local=local), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                first, rendered, scene, meta = self.worker(root, "fine", local)
                first.timeline_sha = digest(first.timeline_file)
                with patch.object(first, "render", return_value=rendered):
                    base_outputs = first.package_review(scene, meta)["outputs"]
                base = media.read_json(Path(base_outputs["version_manifest"]))

                def next_worker(name, outputs, base_version):
                    job = root / name
                    job.mkdir()
                    worker, frames, _, _ = self.worker(job, "standard", local)
                    worker.kind = "revise"
                    worker.order.update(kind="revise", base_version=base_version, target_version=base_version + 1,
                                        review_upload=REJECTED_FRAMES_MODE)
                    worker.sources = {role: Path(path) for role, path in outputs.items()}
                    worker.sources["source_timeline"] = first.timeline_file
                    worker.timeline_sha = digest(first.timeline_file)
                    if local:
                        worker.local_version = worker.sources["version_manifest"].parent.parent
                        worker.sources["scene_bundle"] = worker.local_version / "out/scene.zip"
                        worker.sources["review_bundle"] = worker.local_version / "out/review.zip"
                    return worker, frames

                revision, frames = next_worker("revision", base_outputs, 1)
                revision.load_previous()
                freeze = dict(job_id=base["job_id"], base_version=1, original_annotator_id="alice",
                              timeline_sha256=base["timeline_sha256"],
                              base_manifest_sha256=digest(revision.sources["version_manifest"]))
                manifests = {
                    "feedback_manifest": dict(freeze, frames=[base["frames"][1]], review_size=base["review_size"]),
                    "approval_manifest": dict(freeze, source_sha256=base["source_sha256"], scene_sha256=base["scene_sha256"],
                                              frame_count=base["frame_count"], scope="approved_subset", frames=[base["frames"][0]]),
                }
                for role, data in manifests.items():
                    revision.sources[role] = revision.root / (role + ".json")
                    media.write_json(revision.sources[role], data)
                Image.new("RGB", revision.review_size, "red").save(frames / "0002.png")
                Image.new("RGB", revision.review_size, "blue").save(frames / "0001.png")
                with patch.object(revision, "author_and_validate"), patch.object(revision, "render", return_value=frames):
                    outputs = revision.create_review(None)["outputs"]

                version = media.read_json(Path(outputs["version_manifest"]))
                delta = version["review_delta"]
                self.assertEqual(delta["frames"], [1, 2])
                self.assertEqual(delta["base_manifest_sha256"], freeze["base_manifest_sha256"])
                self.assertEqual(version["review_bundle_sha256"], digest(Path(outputs["review_bundle"])))
                self.assertEqual([v["number"] for v in version["frames"]], [1, 2, 3])
                self.assertEqual(version["frames"][2], base["frames"][2])
                for index in (0, 1):
                    self.assertNotEqual(version["frames"][index]["review_signature"], base["frames"][index]["review_signature"])

                delta_path = root / "delta"
                delta_index = bundles.extract(Path(outputs["review_bundle"]), delta_path, indexed=True)
                self.assertEqual(delta_index.pop("review_delta"), delta)
                self.assertEqual(delta_index["frames"], version["frames"])
                expected = {"pairs/0001.jpg", "pairs/0002.jpg"}
                if not local:
                    expected |= {"white/0001.png", "white/0002.png"}
                self.assertEqual({file["path"] for file in delta_index.pop("files")}, expected)
                merged = root / "merged"
                bundles.extract(Path(base_outputs["review_bundle"]), merged, indexed=True)
                for path in expected:
                    (merged / path).write_bytes((delta_path / path).read_bytes())
                self.assertEqual(object_hash(bundles.entries(merged)), delta["full_files_sha256"])
                if not local:
                    full = root / "merged.zip"
                    bundles.build(merged, delta_index, full)
                    outputs["review_bundle"] = str(full)
                followup, _ = next_worker("followup", outputs, 2)
                followup.load_previous()
                self.assertEqual(followup.previous, version)

    def test_oss_direct_review_preserves_jpg_bytes_and_frozen_base(self):
        def files_for(worker, outputs):
            paths = {role: Path(path) for role, path in outputs.items()}
            artifacts = {role: dict(size=path.stat().st_size, sha256=digest(path)) for role, path in paths.items()}
            return oss_delivery.object_files(worker.request["assignment"], artifacts, paths, Event())

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, rendered, scene, meta = self.worker(root, "fine", True, objects=True)
            first.request["assignment"].update(kind="prepare", input_manifest_sha256="a" * 64)
            first.timeline.update(source_sha256="c" * 64)
            media.write_json(first.timeline_file, dict(source_sha256=first.timeline["source_sha256"],
                                                       frames=[dict(number=n, pts=n-1) for n in range(1, 4)],
                                                       end_pts=3, time_base="1/24"))
            first.timeline_sha = digest(first.timeline_file)
            with patch.object(first, "render", return_value=rendered):
                base_outputs = first.package_review(scene, meta)["outputs"]
            base = media.read_json(Path(base_outputs["version_manifest"]))
            first_files = files_for(first, base_outputs)
            self.assertEqual({name for name in first_files if name.startswith("pairs/")},
                             {"pairs/000001.jpg", "pairs/000002.jpg", "pairs/000003.jpg"})
            self.assertIn("timeline.json", first_files)

            second = root / "revision"
            second.mkdir()
            revision, rendered, _, _ = self.worker(second, "economy", True, objects=True)
            revision.request["assignment"].update(kind="revise", input_manifest_sha256="b" * 64,
                                                 oss=dict(protocol=OSS_OBJECTS, review_scope="rejected_frames"))
            revision.kind = "revise"
            revision.order.update(kind="revise", base_version=1, target_version=2)
            revision.timeline, revision.timeline_sha = first.timeline, first.timeline_sha
            revision.sources = {role: Path(path) for role, path in base_outputs.items()}
            revision.sources["scene_bundle"] = first.output / "scene.zip"
            revision.local_version = first.root
            revision.load_previous()
            freeze = dict(job_id=base["job_id"], base_version=1, original_annotator_id="alice",
                          timeline_sha256=base["timeline_sha256"], base_manifest_sha256=first_files["version.json"].sha256)
            for role, manifest in {
                "approval_manifest": dict(freeze, source_sha256=base["source_sha256"], scene_sha256=base["scene_sha256"],
                                          frame_count=base["frame_count"], scope="approved_subset", frames=base["frames"][:1]),
                "feedback_manifest": dict(freeze, review_size=base["review_size"], frames=[base["frames"][1]]),
            }.items():
                revision.sources[role] = second / (role + ".json")
                media.write_json(revision.sources[role], manifest)
            Image.new("RGB", revision.review_size, "red").save(rendered / "0001.png")
            with patch.object(revision, "author_and_validate"), patch.object(revision, "render", return_value=rendered):
                revised = revision.create_review(None)["outputs"]
            files = files_for(revision, revised)
            pairs = ("pairs/000001.jpg", "pairs/000002.jpg")
            self.assertEqual(set(files), {"review-index.json", "version.json", "production.json", "meta.jsonl", *pairs})
            index = media.read_json(files["review-index.json"].path)
            version = media.read_json(files["version.json"].path)
            self.assertEqual(index["frames"], [dict(number=n, **files[path].declaration()) for n, path in enumerate(pairs, 1)])
            self.assertEqual(index["base_manifest_sha256"], first_files["version.json"].sha256)
            self.assertEqual(version["review_index_sha256"], files["review-index.json"].sha256)
            self.assertEqual(version["review_media"], OSS_OBJECTS)
            self.assertEqual(version["frames"][1:], base["frames"][1:])
            self.assertNotEqual(version["frames"][0]["review_signature"], base["frames"][0]["review_signature"])
            with zipfile.ZipFile(revised["review_bundle"]) as local:
                for path in ("pairs/000002.jpg", "pairs/000003.jpg"):
                    self.assertEqual(local.read(path), first_files[path].path.read_bytes())
            oss_delivery.package_manual_objects(revision.request["assignment"], second, files, None, Event(), Mock())
            with zipfile.ZipFile(second / "submission.zip") as manual:
                self.assertEqual(set(manual.namelist()), set(files))
                for path in pairs:
                    self.assertEqual(manual.read(path), files[path].path.read_bytes())

    def test_auditor_starts_on_first_pair_and_approval_binds_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            worker, frame_folder, scene, meta = self.worker(root, "standard", True, dual=True)
            first = [media.rgb(frame_folder / f"{n:04d}.png") for n in (2, 3)]
            for n in (2, 3):
                (frame_folder / f"{n:04d}.png").unlink()

            def inspect(_, directory, fingerprint, cancelled, deadline, *, preview):
                index = directory / "frames.json"
                payload = json.loads(index.read_text(encoding="utf-8"))
                self.assertEqual(payload["available"], [1])
                self.assertTrue((directory / "F00001.jpg").is_file())
                started.set()
                while not payload["complete"]:
                    if cancelled.wait(.05):
                        raise AssertionError("audit cancelled")
                    payload = json.loads(index.read_text(encoding="utf-8"))
                return json.dumps({"verdict": "pass", "inspected": payload["available"], "issues": [],
                                   "review_token": payload["review_token"]})

            def render(scene, target, purpose, *, on_frames):
                on_frames(frame_folder)
                self.assertTrue(started.wait(5))
                for n, image in enumerate(first, 2):
                    image.save(frame_folder / f"{n:04d}.png")
                on_frames(frame_folder)
                return frame_folder

            started = Event()
            with patch.object(worker, "render", side_effect=render), patch.object(worker, "run_auditor", side_effect=inspect):
                result = worker.package_review(scene, meta)
            self.assertEqual(result["automatic_review"]["outputs_sha256"]["review_bundle"],
                             digest(Path(result["outputs"]["review_bundle"])))

    def test_missing_evidence_cannot_approve(self):
        with self.assertRaises(visual_review.VisualReviewError):
            visual_review.conclusion('{"verdict":"pass","inspected":[1],"issues":[]}',
                                     count=3, required=[1, 2], available=[1, 2])

    def test_exhausted_visual_repair_submits_full_package_for_human_review(self):
        issues = [{"start": 2, "end": 2, "issue": "头部朝向需要人工复核"}]
        for stage in ("preview", "review", "review_finish"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                directory = root / "work" / "work_test" / "1"
                directory.mkdir(parents=True)
                worker, rendered, scene, meta = self.worker(directory, "standard", False, dual=True)
                rejection = visual_review.RevisionNeeded(issues)

                def render(_, __, purpose, *, on_frames=None):
                    self.assertEqual(purpose, "review")
                    if on_frames:
                        on_frames(rendered)
                    return rendered

                with (patch.object(worker, "author_and_validate"), patch.object(worker, "repair_visual") as repair,
                      patch.object(worker, "preview_review", side_effect=rejection if stage == "preview" else None),
                      patch.object(worker, "render", side_effect=render), patch.object(visual_review, "VisualReview") as review):
                    review.return_value.poll.side_effect = rejection if stage == "review" else None
                    review.return_value.finish.side_effect = rejection if stage == "review_finish" else None
                    result = worker.create_review(None)
                self.assertEqual(repair.call_count, visual_review.MAX_REPAIRS)
                self.assertNotIn("automatic_review", result)
                handoff = result["human_review"]
                self.assertEqual((handoff["stage"], handoff["issues"]), ("preview" if stage == "preview" else "review", issues))
                self.assertEqual(handoff["scene_sha256"], digest(scene))
                with zipfile.ZipFile(result["outputs"]["review_bundle"]) as archive:
                    self.assertEqual([name for name in archive.namelist() if name.startswith("pairs/")],
                                     ["pairs/0001.jpg", "pairs/0002.jpg", "pairs/0003.jpg"])

                scheduler = Scheduler.__new__(Scheduler)
                scheduler.root, scheduler.state, scheduler.cfg = root, State(root), {"transfer_slots": 1}
                assignment = dict(job_id="1", work_id="work_test", kind="prepare", lease={"attempt": 1},
                                  input_manifest=worker.order, input_manifest_sha256="fixture")
                scheduler.state.start(assignment)
                atomic_json(directory / "review-settings.json", {"dual_role_review": True})
                checkpoint = dict(result, attempt=1, outputs={})
                for role, raw in result["outputs"].items():
                    path = Path(raw)
                    checkpoint["outputs"][role] = dict(name=str(path.relative_to(directory)), size=path.stat().st_size,
                                                        sha256=digest(path))
                transfer = Mock(stop=Event(), work="work_test")
                transfer.upload.side_effect = lambda role, path, *_args, **_kwargs: {"role": role, "sha256": digest(path)}
                payload = scheduler.upload_result(assignment, worker.profile, checkpoint, directory, transfer)
                self.assertEqual({item["role"] for item in payload["outputs"]}, set(result["outputs"]))
                transfer.upload.reset_mock()
                handoff["issues"] = [{"start": 1, "end": 1, "issue": "不匹配的审核意见"}]
                with self.assertRaisesRegex(WorkFailure, "制作报告缺少匹配"):
                    scheduler.upload_result(assignment, worker.profile, checkpoint, directory, transfer)
                transfer.upload.assert_not_called()

    def test_invalid_audit_evidence_remains_blocked_after_two_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker, _, _, _ = self.worker(root, "standard", False, dual=True)
            issues = [{"start": 2, "end": 2, "issue": "姿态偏差"}]
            preview = [visual_review.RevisionNeeded(issues) for _ in range(visual_review.MAX_REPAIRS)]
            preview.append(visual_review.VisualReviewError("独立审核证据不足"))
            with (patch.object(worker, "author_and_validate"), patch.object(worker, "repair_visual"),
                  patch.object(worker, "preview_review", side_effect=preview), patch.object(worker, "package_review") as package):
                with self.assertRaisesRegex(visual_review.VisualReviewError, "独立审核证据不足"):
                    worker.create_review(None)
            package.assert_not_called()
