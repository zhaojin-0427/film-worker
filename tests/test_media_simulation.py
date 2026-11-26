"""Exercise real artifact production and return transfer against the local contract fixture."""
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from film_worker import intake, media
from film_worker.processes import ProcessTree
from film_worker.protocol import LOCAL_REVIEW_MODE
from film_worker.scheduler import Scheduler
from film_worker.settings import DEFAULTS, MODEL_FIELDS, ROOT, resolve_tools
from test_server_flow import mock_service, node_config, sha


REFERENCES = ("version_manifest","scene_bundle","shot_metadata","review_bundle","production_report")


def tool_overrides():
    return {name: os.environ[f"FILM_TEST_{name.upper()}"] for name in ("blender","codex","ffmpeg","ffprobe")
            if os.environ.get(f"FILM_TEST_{name.upper()}")}


def json_blob(data):
    return json.dumps(data,ensure_ascii=False).encode()


def start_fixture(command, *, cwd, env, log):
    worker = Path(__file__).with_name("media_child.py")
    return ProcessTree([command[0],str(worker),command[-1]],cwd=cwd,env={**env,"PYTHONPATH":str(ROOT)},log=log)


def source_clip(ffmpeg, path, *, variable):
    duration = "0.16667" if variable else "0.125"
    select = ["-vf", r"select=not(eq(n\,1))"] if variable else []
    subprocess.run([ffmpeg,"-v","error","-f","lavfi","-i",f"testsrc2=size=640x360:rate=24:duration={duration}",
                    "-f","lavfi","-i",f"sine=frequency=440:sample_rate=44100:duration={duration}",
                    *select,"-fps_mode","vfr","-frames:v","3","-c:v","mpeg4","-c:a","aac",str(path)],check=True)


def codex_source_clip(ffmpeg, path):
    scene = "color=c=0x262626:size=640x360:rate=24:duration=0.125,drawbox=x=245:y=105:w=150:h=150:c=white:t=fill"
    subprocess.run([ffmpeg,"-v","error","-f","lavfi","-i",scene,
                    "-f","lavfi","-i","sine=frequency=440:sample_rate=44100:duration=0.125",
                    "-c:v","mpeg4","-c:a","aac",str(path)],check=True)


@unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_SIMULATION")=="1","requires local Blender and media tools")
class WholeMediaSimulation(unittest.TestCase):
    def wait_work(self, service, worker, work, timeout):
        end = time.monotonic()+timeout
        while time.monotonic()<end:
            with service.mutex:
                job = service.attempts[work]
                if job["state"]=="succeeded" and not worker.summary()["active"]:
                    return
                if job["state"]=="failed":
                    directory = worker.root/"work"/work/"1"
                    logs = [directory/name for name in ("worker.log","blender-prepare.log","blender-review-cpu.log",
                                                         "blender-final-cpu.log")]
                    self.fail(f"{job['error']}\n"+"\n".join(
                        f"{log.name}\n{log.read_text(encoding='utf-8',errors='replace')[-1500:]}"
                        for log in logs if log.is_file()))
            time.sleep(0.15)
        self.fail(str(worker.summary()))

    def test_http_roundtrip_with_real_blender_1080p_4k_and_audio(self):
        self.roundtrip(variable=False)

    def test_variable_source_timing_survives_revision_by_another_node_and_4k(self):
        self.roundtrip(variable=True, export_quality="4k")

    def test_finalize_can_select_1080p_after_approval_for_a_4k_native_scene(self):
        self.roundtrip(variable=False, export_quality="1080p")

    def test_same_node_review_uploads_comparisons_only_and_exports_1080p_after_restart(self):
        self.roundtrip(variable=False, export_quality="1080p", local_review=True)

    def test_same_node_review_uses_frozen_local_scene_for_4k(self):
        self.roundtrip(variable=False, export_quality="4k", local_review=True)

    @unittest.skipUnless(os.getenv("FILM_WORKER_TEST_REAL_CODEX")=="1","requires a locally authenticated Codex")
    def test_real_codex_authors_and_revises_work_received_from_the_mock_manager(self):
        model = {field: os.environ["FILM_TEST_"+field.upper()] for field in MODEL_FIELDS}
        self.roundtrip(variable=False,model=model)

    def test_hdr_sources_convert_both_pq_and_hlg_to_sdr_review_frames(self):
        tools = resolve_tools({**DEFAULTS,**tool_overrides()})
        env = {"FILM_WORKER_FFMPEG":tools["ffmpeg"],"FILM_WORKER_FFPROBE":tools["ffprobe"]}
        with tempfile.TemporaryDirectory(prefix="worker-hdr-") as directory, patch.dict(os.environ,env):
            for transfer, sample_x, sdr_value in (("smpte2084",30,54),("arib-std-b67",60,137)):
                with self.subTest(transfer=transfer):
                    root = Path(directory)/transfer
                    root.mkdir()
                    clip = root/"hdr.mkv"
                    ramp = ("nullsrc=size=96x32:rate=25,format=yuv420p10le,"
                            "geq=lum=64+840*X/(W-1):cb=512:cr=512,"
                            f"setparams=range=limited:color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc")
                    subprocess.run([tools["ffmpeg"],"-v","error","-f","lavfi","-i",ramp,
                                    "-frames:v","2","-c:v","ffv1",str(clip)],check=True)
                    timeline = media.prepare_source(root,clip,1920)
                    self.assertEqual(Fraction(timeline["duration"]),Fraction(2,25))
                    frames = sorted((root/"intake/frames").glob("*.png"))
                    self.assertEqual(len(frames),2)
                    for path in frames:
                        with Image.open(path) as im:
                            values = im.getpixel((sample_x,16))
                            for channel in values:
                                self.assertAlmostEqual(channel,sdr_value,delta=3)
                            self.assertLessEqual(im.getpixel((0,16))[0],3)
                            self.assertGreater(im.getpixel((95,16))[0],215)
                    with Image.open(root/"intake/overview.jpg") as review:
                        self.assertEqual(review.width,640)
                        preview = review.getpixel((round(sample_x * 320 / 96), review.height//2))
                        self.assertAlmostEqual(preview[0],sdr_value,delta=6)

    def roundtrip(self, *, variable, model=None, export_quality=None, local_review=False):
        with tempfile.TemporaryDirectory(prefix="worker-local-manager-") as directory, mock_service() as service:
            root = Path(directory)
            data = root/"节点 数据"
            data.mkdir()
            config = node_config(service)
            config["render_backend"] = "cpu"
            config["cpu_threads"] = 2
            config["final_width"] = 3840
            config.update(tool_overrides())
            tools = resolve_tools(config)
            clip = root/"input.mp4"
            if model:
                codex_source_clip(tools["ffmpeg"],clip)
                note = "将白色方块在画面上整体向右移动约 80 个原片像素，三个画面保持相同的缩放。"
            else:
                source_clip(tools["ffmpeg"],clip,variable=variable)
                note = "主体右移"
            source = service.store("source_video",clip.read_bytes(),"video/mp4")
            defaults = {"source_video":source}
            if model:
                config.update(model)

            timeout = 360 if model else 70
            scene_authoring = nullcontext() if model else patch("film_worker.scheduler.ProcessTree",start_fixture)
            with scene_authoring:
                node = Scheduler(config,data,"fixture",test_http=True)
                storage = {"mode": LOCAL_REVIEW_MODE, "instance_id": node.instance_id} if local_review else None
                worker = threading.Thread(target=node.run)
                try:
                    first = service.schedule("prepare",1,artifacts=list(defaults.values()),review_storage=storage)
                    worker.start()
                    self.wait_work(service,node,first,timeout)
                    self.assertEqual(service.completed,[first],"人工审核前应等待")
                    artifacts = service.blobs_from(first)
                    version1 = json.loads(artifacts["version_manifest"])
                    frame1 = version1["frames"][1]
                    timeline = service.outputs_from(first,"source_timeline")
                    clock = json.loads(artifacts["source_timeline"])
                    self.assertIs(clock["variable_rate"],variable)
                    base = Fraction(clock["time_base"])
                    durations = [Fraction(n,24) for n in ((2,1,1) if variable else (1,1,1))]
                    self.assertEqual([f["duration_pts"]*base for f in clock["frames"]],durations)
                    self.assertEqual(version1["review_size"],{"width":1920,"height":1080})
                    self.assertEqual(len(version1["frames"]),3)
                    with zipfile.ZipFile(io.BytesIO(artifacts["review_bundle"])) as reviews:
                        self.assertIn(frame1["pair_jpeg_path"],reviews.namelist())
                        self.assertEqual(frame1["white_png_path"] in reviews.namelist(),not local_review)
                        if local_review:
                            self.assertEqual(set(reviews.namelist()),{"index.json",*(f["pair_jpeg_path"] for f in version1["frames"])})
                            self.assertEqual(json.loads(reviews.read("index.json"))["review_storage"],storage)
                    if local_review:
                        self.assertEqual(version1["review_storage"],storage)
                        self.assertNotIn("scene_bundle",artifacts)
                        retained = data/"work"/first/"1"
                        self.assertTrue((retained/"out/scene.zip").is_file())
                        for frame in version1["frames"]:
                            self.assertTrue((retained/"local-review"/frame["white_png_path"]).is_file())

                    status = dict(schema_version=1,job_id="job1",base_version=1,original_annotator_id="alice",
                                  base_manifest_sha256=sha(artifacts["version_manifest"]),
                                  timeline_sha256=version1["timeline_sha256"])
                    feedback = dict(status,origin="review",review_size=version1["review_size"],
                                    frames=[dict(number=2,review_signature=frame1["review_signature"],
                                                note=note,regions=[dict(x=0.3,y=0.64,w=0.4,h=0.3,note="核对主体位置")])])
                    feedback_artifact = service.store("feedback_manifest",json_blob(feedback),"application/json")
                    approve = self.approval(service,version1,status,all_frames=False)
                    if variable or local_review:
                        node.stop.set()
                        worker.join(timeout=20)
                        self.assertFalse(worker.is_alive())
                        if local_review:
                            node = Scheduler(config,data,"fixture",test_http=True)
                            self.assertEqual(node.instance_id,storage["instance_id"])
                        else:
                            other = root/"另一台节点"
                            other.mkdir()
                            node = Scheduler({**config,"node_key":"fixture-handoff"},other,"fixture",test_http=True)
                        worker = threading.Thread(target=node.run)
                        worker.start()
                    references = ("version_manifest","shot_metadata") if local_review else REFERENCES
                    revised = service.schedule("revise",2,artifacts=[*defaults.values(),*timeline,
                        *service.outputs_from(first,*references),feedback_artifact,approve],review_storage=storage)
                    self.wait_work(service,node,revised,timeout)
                    self.assertEqual(service.workers["fixture"]!=node.session["worker_id"],variable)
                    self.assertTrue((node.root/"work"/revised/"1/scene/feedback-images/0002.jpg").is_file())
                    new = service.blobs_from(revised)
                    version2 = json.loads(new["version_manifest"])
                    if local_review:
                        self.assertNotIn("scene_bundle",new)
                        self.assertEqual(version2["review_storage"],storage)
                        with zipfile.ZipFile(io.BytesIO(new["review_bundle"])) as reviews:
                            self.assertEqual(set(reviews.namelist()),{"index.json",*(f["pair_jpeg_path"] for f in version2["frames"])})
                    changes = [before["number"] for before,after in zip(version1["frames"],version2["frames"],strict=True)
                               if before["review_signature"]!=after["review_signature"]]
                    if model:
                        self.assertIn(2,changes)
                        for recorded in (version1,version2):
                            self.assertEqual(recorded["environment"]["model_evidence"],"observed")
                            for field, expected in model.items():
                                self.assertEqual(recorded["environment"][field],expected)
                        carried = {1}-set(changes)
                    else:
                        self.assertEqual(changes,[2,3] if variable else [2])
                        carried = {1}
                    self.assertEqual(version1["source_sha256"],version2["source_sha256"])
                    self.assertFalse(any(w=="work-finalize" for w in service.completed),"全部通过前不得出片")
                    status.update(base_version=2,base_manifest_sha256=sha(new["version_manifest"]))
                    approve = self.approval(service,version2,status,all_frames=True,carried=carried)
                    export = service.schedule("finalize",2,artifacts=[*defaults.values(),*timeline,
                        *service.outputs_from(revised,*references),approve],drop_reply=True,export_quality=export_quality,
                        review_storage=storage)
                    self.wait_work(service,node,export,timeout)
                    result = service.blobs_from(export)
                    receipt = service.attempts[export]["body"]["final_verification"]
                    hd = export_quality == "1080p"
                    width, height = (1920,1080) if hd else (3840,2160)
                    self.assertEqual(receipt["white_size"],{"width":width,"height":height})
                    self.assertEqual(receipt["comparison_size"],{"width":width,"height":height*2})
                    self.assertEqual(receipt["render_source"],"approved_native_white" if hd else "locked_scene_native_render")
                    self.assertEqual(receipt["verified_review_frames"],3)
                    self.assertTrue(service.replayed[export])
                    for role in ("white_video","comparison_video"):
                        output = root/(role+".mp4")
                        output.write_bytes(result[role])
                        with patch.dict(os.environ,{"FILM_WORKER_FFPROBE":tools["ffprobe"]}):
                            _,video,pts,timescale,duration,_ = intake.measure(output)
                        self.assertEqual((video["width"],video["height"]),(width,height if role=="white_video" else height*2))
                        self.assertEqual([n*timescale for n in pts],[f["pts"]*base for f in clock["frames"]])
                        self.assertEqual(duration,sum(durations))
                    with zipfile.ZipFile(io.BytesIO(result["delivery_bundle"])) as deliver:
                        self.assertEqual(deliver.read("target/source.mp4"),clip.read_bytes())
                        params=[json.loads(row) for row in deliver.read("meta/video-technical.jsonl").splitlines()]
                        self.assertEqual([i["role"] for i in params],["source_video","white_video","comparison_video"])
                        self.assertEqual(params[1]["audio"][0]["sample_rate"],48000)
                    self.assertEqual(service.completed,[first,revised,export])
                finally:
                    node.stop.set()
                    if worker.ident:
                        worker.join(timeout=20)

    def approval(self, service, version, status, *, all_frames, carried=()):
        subset = version["frames"] if all_frames else [version["frames"][0]]
        entry = dict(status,scope="all_frames" if all_frames else "approved_subset",
                     **{k:version[k] for k in ("source_sha256","scene_sha256","frame_count")})
        entry["frames"] = [dict(number=f["number"],review_signature=f["review_signature"],
                                approved_version=1 if f["number"] in carried else version["version"]) for f in subset]
        return service.store("approval_manifest",json_blob(entry),"application/json")
