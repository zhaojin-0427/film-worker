import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image, JpegImagePlugin

from film_worker import media, production, visual_review
from film_worker.protocol import digest
from film_worker.settings import REVIEW_JPEG, validate_config


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
