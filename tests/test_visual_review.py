import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image, JpegImagePlugin

from film_worker import bundles, media, production, visual_review
from film_worker.protocol import REJECTED_FRAMES_MODE, digest
from film_worker.settings import REVIEW_JPEG, object_hash, validate_config


class ReviewTests(unittest.TestCase):
    def worker(self, root, clarity, local, dual=False):
        order = {"kind": "prepare", "job_id": "1", "target_version": 1}
        if local:
            order["review_storage"] = {"mode": "node_local_v1", "instance_id": "fixture"}
        worker = production.Production(root, dict(
            assignment={"input_manifest": order, "input_manifest_sha256": "fixture"},
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

    def test_revision_uploads_only_rejected_frames_and_loads_the_merged_base(self):
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
                with patch.object(revision, "author_and_validate"), patch.object(revision, "render", return_value=frames):
                    outputs = revision.create_review(None)["outputs"]
                    Image.new("RGB", revision.review_size, "blue").save(frames / "0003.png")
                    with self.assertRaisesRegex(ValueError, "打回范围外"):
                        revision.create_review(None)

                version = media.read_json(Path(outputs["version_manifest"]))
                delta = version["review_delta"]
                self.assertEqual(delta["frames"], [2])
                self.assertEqual(delta["base_manifest_sha256"], freeze["base_manifest_sha256"])
                self.assertEqual(version["review_bundle_sha256"], digest(Path(outputs["review_bundle"])))
                self.assertEqual([v["number"] for v in version["frames"]], [1, 2, 3])
                for index in (0, 2):
                    self.assertEqual(version["frames"][index], base["frames"][index])
                self.assertNotEqual(version["frames"][1]["review_signature"], base["frames"][1]["review_signature"])

                delta_path = root / "delta"
                delta_index = bundles.extract(Path(outputs["review_bundle"]), delta_path, indexed=True)
                self.assertEqual(delta_index.pop("review_delta"), delta)
                self.assertEqual(delta_index["frames"], version["frames"])
                expected = {"pairs/0002.jpg"} if local else {"pairs/0002.jpg", "white/0002.png"}
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
