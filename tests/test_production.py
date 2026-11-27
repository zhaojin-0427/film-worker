"""Real Blender and FFmpeg round trip with a deterministic authoring fixture."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from film_worker import draft_review, media, production, render_resume
from film_worker.production import Production
from film_worker.protocol import digest
from film_worker.settings import DEFAULTS, atomic_json, resolve_tools


class ProductionTests(unittest.TestCase):
    def test_new_attempt_reuses_matching_scene_and_renders_only_missing_review_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "work" / "work-fixture"
            assignment = dict(work_id=work.name, job_id="1", input_manifest_sha256="a" * 64,
                              input_manifest=dict(kind="prepare", job_id="1", target_version=1))
            common = dict(paths={}, tools={"blender": "blender"}, cpu_threads=2,
                          environment=dict(render_backend="optix", compatibility={"blender": "5.2.3", "decoder_id": "test"}))

            def make_worker(attempt, *, input_sha=assignment["input_manifest_sha256"], threads=2):
                folder = work / str(attempt)
                folder.mkdir(parents=True)
                request = dict(common, cpu_threads=threads, assignment=dict(
                    assignment, input_manifest_sha256=input_sha, lease=dict(attempt=attempt, worker_id="fixture")))
                atomic_json(folder / "request.json", request)
                worker = Production(folder, request)
                worker.count = 5
                worker.review_size = worker.final_size = (32, 18)
                worker.previous = None
                return worker

            first = make_worker(1)
            scene, meta = (first.stage / name for name in ("scene.blend", "meta.jsonl"))
            scene.write_bytes(b"prepared scene")
            meta.write_text('{"start_frame":1,"end_frame":5,"description":"shot"}\n', encoding="utf-8")
            atomic_json(first.root / "prepared.json", dict(input=assignment["input_manifest_sha256"],
                        scene=digest(scene), meta=digest(meta), environment=first.env))
            frames = first.root / "review-optix"
            atomic_json(frames / "checkpoint.json", first.render_tag(digest(scene), first.review_size, "optix"))
            for number in (1, 2, 4):
                Image.new("RGB", first.review_size, "red").save(media.frame_path(frames, number))
            media.frame_path(frames, 3).write_bytes(b"incomplete PNG")
            (frames / "rendering.png").write_bytes(b"temporary render")
            before = {file.name: digest(file) for file in frames.iterdir()}

            retry = make_worker(2)
            missing = []

            def render_missing(_name, _command, *, frames, **kwargs):
                self.assertEqual(json.loads((retry.root / "progress.json").read_bytes())["completed"], 3)
                for number in range(1, retry.count + 1):
                    frame = media.frame_path(frames, number)
                    if not frame.exists():
                        missing.append(number)
                        Image.new("RGB", retry.review_size, "blue").save(frame)

            def package(restored_scene, restored_meta):
                self.assertEqual(restored_scene.read_bytes(), scene.read_bytes())
                self.assertEqual(restored_meta.read_bytes(), meta.read_bytes())
                return retry.render_locked(restored_scene, retry.review_size, "review")

            with (patch.object(retry, "author_and_validate") as author,
                  patch.object(retry, "package_review", side_effect=package),
                  patch.object(retry, "run_tool", side_effect=render_missing)):
                result = retry.create_review(None)
            author.assert_not_called()
            self.assertEqual(missing, [3, 5])
            self.assertEqual(json.loads((retry.root / "progress.json").read_bytes())["completed"], 5)
            for number in (1, 2, 4):
                self.assertEqual(digest(media.frame_path(result, number)), before[f"{number:04d}.png"])
            self.assertFalse((result / "rendering.png").exists())
            self.assertEqual({file.name: digest(file) for file in frames.iterdir()}, before)

            for attempt, options in ((3, {"input_sha": "b" * 64}), (4, {"threads": 8})):
                incompatible = make_worker(attempt, **options)
                self.assertEqual(render_resume.previous_attempts(incompatible.root, incompatible.request), [])

    @unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_MEDIA") == "1", "requires Blender and FFmpeg")
    def test_bundled_first_draft_repair_and_full_timeline_self_review(self):
        tools = resolve_tools(DEFAULTS)
        with tempfile.TemporaryDirectory(prefix="film-bundled-draft-") as raw:
            root = Path(raw)
            clip = root / "input.mp4"
            subprocess.run([tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i",
                            "color=s=160x90:r=24", "-frames:v", "3", "-c:v", "mpeg4", str(clip)], check=True)
            work = root / "work"
            work.mkdir()
            order = dict(kind="prepare", job_id="fixture", target_version=1, artifacts=[
                dict(role="source_video", media_type="video/mp4")])
            request = dict(assignment=dict(input_manifest=order, input_manifest_sha256="0"*64), tools=tools,
                           paths={"source_video": str(clip)}, environment=dict(render_backend="cpu"),
                           production=dict(final_width=320, compatibility={"decoder_id":"test"},
                                           review_landscape_limit={"width":1920,"height":1080}),
                           resources=str(root / "slots"), cpu_threads=2, model_slots=1, render_slots=1)
            worker = Production(work, request)
            stages = []

            def fixture(prompt, name, **options):
                stages.append(name)
                self.assertIn("制作 SOP", prompt)
                self.assertIn(str(production.BODY_MODEL), prompt)
                self.assertNotIn("workflow_bundle", prompt)
                if name == "codex-self-review":
                    self.assertTrue(draft_review.current(worker.stage))
                    self.assertTrue(options["context_hash"])
                    (worker.stage / "制作记录.md").write_text("已复查 F1–3", encoding="utf-8")
                    return
                if name == "codex-repair":
                    self.assertIn("后台制作只使用 Cycles", prompt)
                    (worker.stage / "scene_details.py").unlink()
                else:
                    (worker.stage / "scene_details.py").write_text('scene.render.engine = "BLENDER_EEVEE"\n')
                plan = dict(objects={"person": {"kind":"actor","palette_role":"lead"}},
                            shots=[dict(start=1, end=3, description="人物",
                                        camera=[dict(f=1, eye=[0,-4,1], aim=[0,0,1])],
                                        tracks={"person":[{"f":1,"at":[0,0,0]}]})])
                media.write_json(worker.stage / "shot-plan.json", plan)

            with (patch.dict(os.environ, {"FILM_WORKER_FFMPEG":tools["ffmpeg"], "FILM_WORKER_FFPROBE":tools["ffprobe"]}),
                  patch.object(worker, "run_model", side_effect=fixture)):
                result = worker.run()
            self.assertEqual(stages, ["codex", "codex-repair", "codex-self-review"])
            manifest = media.read_json(Path(result["outputs"]["version_manifest"]))
            self.assertNotIn("profile_hash", manifest)
            self.assertEqual(manifest["frame_count"], 3)
            with zipfile.ZipFile(result["outputs"]["scene_bundle"]) as saved:
                self.assertIn(".actor-geometry.json", saved.namelist())
            snapshot = worker.stage / ".actor-geometry.json"
            legacy = root / "legacy-actor-geometry.json"
            media.write_json(legacy, media.read_json(snapshot)["geometry"])
            worker.run_tool("test-upgrade-actor-baseline", worker.blender_command(
                "-b", "--disable-autoexec", str(worker.stage / "scene.blend"), "--python-exit-code", "1",
                "-P", str(Path(production.__file__).with_name("actor_guard.py")), "--", str(legacy)))
            self.assertEqual(media.read_json(legacy), media.read_json(snapshot))
            guard_test = root / "check-actor-appearance.py"
            guard_test.write_text("""
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, sys.argv[-2])
from actor_guard import assert_unchanged
from engineering_checks import EngineeringValidationError

scene = bpy.context.scene
snapshot = json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8"))
assert snapshot["schema_version"] == 2
body = next(obj for obj in scene.objects if obj.get("movie_actor_body"))

def check_blocked():
    try:
        assert_unchanged(scene, snapshot)
    except EngineeringValidationError:
        return
    raise AssertionError("appearance change passed the guard")

body.rotation_euler.x += 0.15
assert_unchanged(scene, snapshot)
material = body.material_slots[0].material
shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
roughness = shader.inputs["Roughness"]
original = roughness.default_value
roughness.default_value = 0.1 if original > 0.5 else 0.8
check_blocked()
roughness.default_value = original
assert_unchanged(scene, snapshot)

bpy.ops.mesh.primitive_cube_add()
added = bpy.context.object
added.parent = body
check_blocked()
added.parent = None
assert_unchanged(scene, snapshot)
added.constraints.new("COPY_TRANSFORMS").target = body
check_blocked()
""", encoding="utf-8")
            worker.run_tool("test-actor-appearance", worker.blender_command(
                "-b", "--disable-autoexec", str(worker.stage / "scene.blend"), "--python-exit-code", "1",
                "-P", str(guard_test), "--", str(Path(production.__file__).parent),
                str(snapshot)))

    @unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_MEDIA") == "1", "requires Blender, FFmpeg and FFprobe")
    def test_prepare_revise_finalize_from_frozen_artifacts(self):
        tools = resolve_tools(DEFAULTS)
        with tempfile.TemporaryDirectory(prefix="film-worker-integration-") as root:
            base = Path(root)
            src = base / "source.mp4"
            subprocess.run([tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.125",
                            "-c:v", "mpeg4", "-c:a", "aac", str(src)], check=True)
            paths = {"source_video": str(src)}
            for role in ("rules_bundle", "workflow_bundle"):
                artifact = base / (role + ".zip")
                with zipfile.ZipFile(artifact, "w") as bundle:
                    bundle.writestr("fixture.txt", "production fixture")
                paths[role] = str(artifact)
            full = os.environ.get("FILM_WORKER_TEST_FULL_SIZE") == "1"
            fw,fh,rw,rh = (3840,2160,1920,1080) if full else (1280,720,960,540)
            profile = dict(final_width=fw, review_landscape_limit=dict(width=rw,height=rh),
                           review_portrait_limit=dict(width=rh,height=rw), profile_hash="f"*64,
                           compatibility=dict(decoder_id="fixture"))

            def run(kind, previous, version, inputs, attempt=""):
                directory = base / ("attempt-" + kind + attempt)
                directory.mkdir()
                fixtures = {role: Path(path) for role, path in inputs.items()}
                artifacts = [dict(role=k, media_type="video/mp4" if k=="source_video" else "application/octet-stream")
                             for k in fixtures]
                order = dict(kind=kind, profile=profile, artifacts=artifacts, job_id="job1",
                             base_version=previous, target_version=version)
                spec = dict(input_manifest=order, input_manifest_sha256="0"*64)
                request = dict(assignment=spec, paths=inputs, tools=tools,
                               environment=dict(render_backend="cpu"), resources=str(base/"slots"),
                               cpu_threads=2, model_slots=1, render_slots=1)
                with patch.dict(os.environ, {"FILM_WORKER_FFMPEG":tools["ffmpeg"],
                                              "FILM_WORKER_FFPROBE":tools["ffprobe"]}), patch.object(Production, "author", author_fixture):
                    return Production(directory, request).run()

            first = run("prepare", None, 1, paths)["outputs"]
            orig = media.read_json(Path(first["version_manifest"]))
            self.assertEqual(orig["frame_count"], 3)
            self.assertEqual(orig["review_size"], dict(width=rw,height=rh))
            edit_inputs = {**paths, **{k:v for k,v in first.items() if k!="production_report"}}
            approval = base/"approval.json"
            feedback = base/"feedback.json"
            original_sha = hashlib.sha256(Path(first["version_manifest"]).read_bytes()).hexdigest()
            origin = dict(job_id="job1", base_version=1, base_manifest_sha256=original_sha,
                          original_annotator_id="alice", timeline_sha256=orig["timeline_sha256"])
            media.write_json(approval, dict(origin, scope="approved_subset", source_sha256=orig["source_sha256"],
                                            profile_hash=orig["profile_hash"], scene_sha256=orig["scene_sha256"],
                                            frame_count=3, frames=[]))
            frame = orig["frames"][1]
            media.write_json(feedback, dict(origin, review_size=orig["review_size"],
                    frames=[dict(number=2, review_signature=frame["review_signature"], note="调整站位", regions=[])]))
            edit_inputs.update(approval_manifest=str(approval), feedback_manifest=str(feedback))
            second = run("revise", 1, 2, edit_inputs)["outputs"]
            revised = media.read_json(Path(second["version_manifest"]))
            self.assertNotEqual(orig["frames"][1]["review_signature"], revised["frames"][1]["review_signature"])
            second_sha = hashlib.sha256(Path(second["version_manifest"]).read_bytes()).hexdigest()
            approved = dict(origin, base_version=2, base_manifest_sha256=second_sha,
                    scope="all_frames", source_sha256=revised["source_sha256"], profile_hash=revised["profile_hash"],
                    scene_sha256=revised["scene_sha256"], frame_count=3,
                    frames=[dict(number=f["number"], review_signature=f["review_signature"], approved_version=2)
                            for f in revised["frames"]])
            final_inputs = {**edit_inputs, **second}
            media.write_json(approval, {**approved, "frames": approved["frames"][:2]})
            with self.assertRaisesRegex(ValueError, "审批清单未覆盖全片"):
                run("finalize", 2, 2, final_inputs, "-incomplete")
            media.write_json(approval, approved)
            result = run("finalize", 2, 2, final_inputs)
            check = result["final_verification"]
            self.assertEqual(check["render_source"], "locked_scene_native_render")
            self.assertEqual(check["white_size"], dict(width=fw,height=fh))
            self.assertEqual(check["verified_review_frames"], 3)
            with zipfile.ZipFile(result["outputs"]["delivery_bundle"]) as archive:
                self.assertEqual(archive.read("target/source.mp4"), src.read_bytes())
                lines = archive.read("meta/video-technical.jsonl").splitlines()
                records = [json.loads(row) for row in lines]
                self.assertEqual([r["role"] for r in records], ["source_video", "white_video", "comparison_video"])
                self.assertEqual({r["sample_id"] for r in records}, {"job1"})
                for rendered in records[1:]:
                    track = rendered["audio"][0]
                    self.assertEqual((track["codec"], track["channels"], track["sample_rate"]), ("aac", 2, 48000))


def author_fixture(worker, source):
    preparing = worker.kind == "prepare"
    script = worker.root / "author.py"
    sound_file = worker.stage / "packed-reference.wav"
    if preparing:
        with wave.open(str(sound_file), "wb") as sound:
            sound.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            sound.writeframes(bytes(12000))
    script.write_text(f"""import bpy
s = bpy.context.scene
s.render.engine = "CYCLES"
s.cycles.samples = 1
s.cycles.use_denoising = False
s.frame_start = 1
s.frame_end = {worker.count}
if {preparing!r}:
    sequence = s.sequence_editor_create()
    strips = sequence.strips if hasattr(sequence, "strips") else sequence.sequences
    strip = strips.new_sound("packed reference", {str(sound_file)!r}, 1, 1)
    strip.sound.pack()
    strip.sound.filepath = "//packed-reference.wav"
else:
    subject = bpy.data.objects["Cube"]
    for frame, x in ((1, 0), (2, 1), (3, {int(worker.timeline["variable_rate"])})):
        subject.location.x = x
        subject.keyframe_insert(data_path="location", frame=frame)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath={str(worker.stage / 'scene.blend')!r})
""",encoding="utf-8")
    command = [worker.tools["blender"], "-b", "--disable-autoexec"]
    if not preparing:
        command.append(str(worker.stage/"scene.blend"))
    worker.run_tool("fixture-author", [*command, "--python-exit-code", "1", "-P", str(script)])
    if preparing:
        sound_file.unlink()
    metadata = worker.stage / "meta.jsonl"
    metadata.write_text(json.dumps(dict(start_frame=1,end_frame=worker.count,description="fixture shot"))+"\n")
