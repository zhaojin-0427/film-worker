"""Regression test for Windows draft encoding and the OptiX cache."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from film_worker import draft_review, media, production, shot_draft
from film_worker.engineering_checks import EngineeringValidationError
from film_worker.settings import DEFAULTS, ROOT, resolve_tools


@unittest.skipUnless(os.name == "nt", "Windows review regression")
class WindowsReviewTests(unittest.TestCase):
    def test_codex_prompt_leaves_draft_encoding_to_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            worker = self.worker(source.parent, {
                "blender": "blender.exe", "ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe",
            }, "optix")
            for kind, repair in (("prepare", None), ("prepare", "fixture error"), ("revise", None)):
                with self.subTest(kind=kind, repair=repair):
                    worker.kind = kind
                    prompt = worker.author_prompt(source, repair=repair)
                    if kind == "prepare":
                        self.assertIn("--preview", prompt)
                    self.assertNotIn(str(production.HERE / "draft_review.py"), prompt)
                    self.assertIn("禁止导入 av、PIL 或 Pillow", prompt)
                    self.assertIn(r"'select=eq(n\,0)'", prompt)
                    self.assertIn("'ffprobe.exe' '-v' 'error'", prompt)
                    self.assertIn("fixed_eye + ray * float(d)", prompt)
                    self.assertIn("allow_nan=False", prompt)

    def test_blender_vector_array_error_can_trigger_engineering_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.worker(Path(temp), {}, "optix")
            error = "AttributeError: Vector addition: (Vector + numpy.ndarray) invalid type for this operation"
            command = [sys.executable, "-c", f"import sys; print({error!r}); sys.exit(1)"]
            with self.assertRaisesRegex(EngineeringValidationError, "Blender Vector 和 NumPy 数组"):
                worker.run_tool("compile-draft", command, engineering=True)
            with self.assertRaisesRegex(RuntimeError, "other-tool 退出码 1"):
                worker.run_tool("other-tool", command, engineering=True)

    @unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_OPTIX") == "1", "requires Blender and an OptiX GPU")
    def test_worker_rebuilds_the_draft_and_writes_its_own_optix_cache(self):
        with patch.dict(os.environ, FILM_WORKER_BUNDLED_TOOLS=str(ROOT / "runtime/tools")):
            tools = resolve_tools(DEFAULTS)
        with tempfile.TemporaryDirectory(prefix="film-worker-optix-") as temp:
            root = Path(temp)
            source = root / "source.mp4"
            subprocess.run([
                tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24",
                "-frames:v", "3", "-c:v", "mpeg4", str(source),
            ], check=True)
            worker = self.worker(root, tools, "optix")
            with patch.dict(os.environ, FILM_WORKER_FFMPEG=tools["ffmpeg"], FILM_WORKER_FFPROBE=tools["ffprobe"]):
                worker.timeline = media.prepare_source(root, source, 320)
                shot_draft.prepare(worker.stage, source, root / "intake", worker.timeline, production.BODY_MODEL)
                plan_path = worker.stage / "shot-plan.json"
                plan = {
                    "objects": {"person": {"kind": "actor", "palette_role": "lead"}},
                    "shots": [{
                        "start": 1, "end": 3, "description": "test",
                        "camera": [{"f": 1, "eye": [0, -4, 1.2], "aim": [0, 0, .9]}],
                        "tracks": {"person": [{"f": 1, "at": [0, 0, 0]}]},
                    }],
                }
                media.write_json(plan_path, plan)
                (worker.stage / "scene_details.py").write_text(
                    """import json
import numpy as np
from mathutils import Vector
from pathlib import Path

eye = Vector((1, 2, 3))
ray = Vector((0, 1, 0))
distance = np.linspace(0, 1, 3)[1]
point = eye + ray * float(distance)
coords = [float(x) for x in point]
output = Path(__file__).with_name('checked-coordinates.json')
output.write_text(json.dumps({'point': coords, 'frame': int(np.int64(1))}, allow_nan=False))
""", encoding="utf-8",
                )
                reviewed = []

                def review(_, name):
                    self.assertEqual(name, "codex-self-review")
                    self.assertTrue(draft_review.current(worker.stage))
                    coordinates = json.loads((worker.stage / "checked-coordinates.json").read_text())
                    self.assertEqual(coordinates, {"point": [1.0, 2.5, 3.0], "frame": 1})
                    movie = worker.stage / draft_review.MOVIE
                    with media.av.open(str(movie)) as video:
                        self.assertEqual(sum(1 for _ in video.decode(video=0)), worker.count)
                    reviewed.append(media.intake.digest(worker.stage / "scene.blend"))
                    if len(reviewed) == 1:
                        plan["shots"][0]["tracks"]["person"][0]["at"][0] = .12
                        media.write_json(plan_path, plan)
                    (worker.stage / "制作记录.md").write_text("Reviewed the fixture.", encoding="utf-8")

                with patch.object(worker, "run_model", side_effect=review):
                    worker.complete_draft(source, worker.author_prompt(source))

            self.assertEqual(len(set(reviewed)), 2)
            self.assertTrue(draft_review.current(worker.stage))
            self.assertTrue(any((root / ".gpu-cache/optix").glob("*.db")))
            log = (root / "draft-review.log").read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("Unable to initialize the OptiX cache database", log)

    @staticmethod
    def worker(root, tools, backend):
        worker = production.Production(root, {
            "assignment": {"input_manifest": {"kind": "prepare"}},
            "paths": {}, "tools": tools, "environment": {"render_backend": backend},
            "cpu_threads": 8, "resources": str(root / "slots"), "render_slots": 1,
        })
        worker.count = 3
        worker.frames = root / "intake/frames"
        worker.final_size = (320, 180)
        return worker
