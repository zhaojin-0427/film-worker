"""Run the worker scheduler against an HTTP service with an end-to-end lease lifecycle."""
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from film_worker.protocol import Interrupted, LOCAL_REVIEW_MODE, REVIEW_STORAGE_HEADER
from film_worker.scheduler import Scheduler, WorkFailure
from film_worker.settings import DEFAULTS, atomic_json, environment, object_hash


ROLES = {
    "prepare": ["source_timeline", "scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"],
    "revise": ["scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"],
    "finalize": ["white_video", "comparison_video", "delivery_bundle"],
}
VERSIONS = {"blender": "5.2.3", "ffmpeg": "9.0.2", "codex_cli": "0.169.1", "decoder_id": "fixture"}


def now(offset=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=offset)).isoformat()


def sha(content):
    return hashlib.sha256(content).hexdigest()


class ProductionMock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Fixture)
        self.mutex = threading.Lock()
        self.input = b"fixture-input"
        self.profile = dict(compatibility=dict(VERSIONS, runner_commit="a"*40, workflow_sha256=sha(self.input),
                                                rules_sha256=sha(self.input)), final_width=3840, color_policy="sRGB",
                            signature_scheme="pair-rgb8-timing-v1", codex_model="fixture", codex_provider="fixture",
                            reasoning_effort="medium", review_landscape_limit={"width":1920,"height":1080},
                            review_portrait_limit={"width":1080,"height":1920})
        self.profile["profile_hash"] = object_hash(self.profile)
        self.registered = []
        self.review_storage = {}
        self.workers = {}
        self.queue = []
        self.attempts = {}
        self.claims = {}
        self.uploads = {}
        self.parts = {}
        self.objects = {"asset":self.input}
        self.replayed = {}
        self.events = {}
        self.completed = []
        self.commit_calls = {}
        self.hold_commits = set()
        self.hold_uploads = set()
        self.upload_calls = set()
        self.stop_work = {}
        self.heartbeat_interval = 20

    def store(self, role, content, mime):
        identity = f"input-{role}-{sha(content)[:12]}"
        with self.mutex:
            self.objects[identity] = content
        return dict(role=role,artifact_id=identity,sha256=sha(content),size_bytes=len(content),media_type=mime)

    def blobs_from(self, work):
        with self.mutex:
            receipt = self.attempts[work]["receipt"]
            return {r["role"]:self.objects[r["artifact_id"]] for r in receipt["artifacts"]}

    def outputs_from(self, work, *roles):
        with self.mutex:
            receipt = self.attempts[work]["receipt"]
            return [r for r in receipt["artifacts"] if r["role"] in roles]

    def schedule(self, kind, version, *, slug=None, job="job1", attempt=1, drop_reply=False,
                 artifacts=None, export_quality=None, legacy_profile=False, review_storage=None):
        work = f"work-{slug or kind}"
        artifacts = artifacts or [dict(artifact_id="asset", role=role, sha256=sha(self.input), size_bytes=len(self.input),
                          media_type="application/octet-stream")
                     for role in ("source_video",)]
        order = dict(schema_version=1, job_id=job, work_id=work, kind=kind,
                     base_version=None if kind=="prepare" else (version-1 if kind=="revise" else version),
                     target_version=version, artifacts=artifacts)
        if legacy_profile:
            order["profile"] = self.profile
        if export_quality is not None:
            order["export_quality"] = export_quality
        if review_storage is not None:
            order["review_storage"] = review_storage
        assignment = dict(job_id=job, work_id=work, kind=kind, input_manifest=order,
                          input_manifest_sha256=object_hash(order),
                          lease=dict(lease_id=f"lease-{work}-{attempt}", fence=6+attempt,
                                     attempt=attempt, worker_id="node", expires_at=now(85)))
        with self.mutex:
            self.queue.append(assignment)
            self.attempts[work] = {"assignment":assignment, "state":"queued", "drop_reply":drop_reply}
        return work


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, status, content=None, **headers):
        self.send_response(status)
        if content is not None:
            content = content if isinstance(content, bytes) else json.dumps(content).encode()
            self.send_header("Content-Length", str(len(content)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def do_GET(self):
        self.handle_fixture("GET")

    def do_POST(self):
        self.handle_fixture("POST")

    def do_PUT(self):
        self.handle_fixture("PUT")

    def handle_fixture(self, method):
        if self.headers.get("Authorization") != "Bearer fixture":
            return self.answer(401, {"code":"unauthenticated"})
        length = int(self.headers.get("Content-Length",0))
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw) if method=="POST" and raw else {}
        path = self.path.removeprefix("/api/v1/")
        token = self.headers.get("Idempotency-Key")
        server = self.server
        with server.mutex:
            if path=="workers/register":
                server.registered.append(payload)
                worker = server.workers.setdefault(payload.get("node_key", "bearer-node"),f"node-{len(server.workers)+1}")
                server.review_storage[worker,payload["instance_id"]] = self.headers.get(REVIEW_STORAGE_HEADER)
                return self.answer(200, dict(worker_id=worker, server_time=now(), heartbeat_seconds=30,
                                            claim_wait_max_seconds=25, lease_seconds=90, lease_heartbeat_seconds=server.heartbeat_interval,
                                            part_size_bytes=8388608))
            if path.startswith("workers/") and path.endswith("/heartbeat") and path.split("/")[1] in server.workers.values():
                return self.answer(200, dict(server_time=now(), accept_claims=True))
            if path=="work/claim":
                if payload["worker_id"] not in server.workers.values():
                    return self.answer(403,{"code":"worker_unknown"})
                if token not in server.claims:
                    def eligible(candidate):
                        owner = candidate["input_manifest"].get("review_storage")
                        identity = payload["worker_id"], payload["instance_id"]
                        return owner is None or (owner["instance_id"] == identity[1] and
                                                 server.review_storage.get(identity) == LOCAL_REVIEW_MODE)
                    index = next((i for i,candidate in enumerate(server.queue) if eligible(candidate)),None)
                    assignment = server.queue.pop(index) if index is not None else None
                    if assignment:
                        assignment["lease"]["worker_id"] = payload["worker_id"]
                    server.claims[token] = assignment
                assignment = server.claims[token]
                if assignment:
                    server.attempts[assignment["work_id"]]["state"] = "leased"
                    return self.answer(200, assignment)
                return self.answer(204, **{"Retry-After":"1"})
            if path.startswith("jobs/") and "/status?work_id=" in path:
                work = path.split("=")[-1]
                attempt = server.attempts[work]
                state = attempt["state"]
                version = attempt["assignment"]["input_manifest"]["target_version"]
                delivered = "work-finalize" in server.completed
                result = dict(job_id=attempt["assignment"]["job_id"], work_id=work,
                              job_state=state if state in {"paused", "stopped", "deleted"} else ("done" if delivered else "review"),
                              work_state=state, attempt=attempt["assignment"]["lease"]["attempt"],
                              directive=server.stop_work.get(work, "continue"),current_version=version,
                              delivery_versions=[2] if delivered else [],terminal=state in {"succeeded","stopped","deleted"},
                              server_time=now())
                if "receipt" in attempt and state != "deleted":
                    result["receipt"] = attempt["receipt"]
                return self.answer(200, result)
            if path.startswith("artifacts/"):
                ident = path.split("/")[1]
                data = server.objects.get(ident)
                if data is None:
                    return self.answer(404,{"code":"missing"})
                return self.answer(200, data, ETag=f'"{sha(data)}"')
            if path.startswith("uploads/"):
                ident = path.split("/")[1]
                upload = server.uploads[ident]
                chunks = server.parts[ident]
                if "/parts/" in path:
                    part = int(path.rsplit("/",1)[1])
                    start = (part-1)*upload["part_size_bytes"]
                    end = start+len(raw)-1
                    checksum = sha(raw)
                    boundary = f"bytes {start}-{end}/{upload['size_bytes']}"
                    if not (1<=part<=upload["part_count"] and end<upload["size_bytes"] and
                            self.headers.get("X-Part-SHA256")==checksum and
                            self.headers.get("Content-Range")==boundary):
                        return self.answer(422,{"code":"hash_mismatch"})
                    chunks[part] = raw
                    rows = [row for row in upload["received_parts"] if row["part_no"] != part]
                    upload["received_parts"] = rows+[dict(part_no=part,start=start,end=end,sha256=checksum)]
                    return self.answer(204)
                if path.endswith("/complete"):
                    if len(chunks)!=upload["part_count"]:
                        return self.answer(409,{"code":"not_ready"})
                    data = b"".join(chunks[i] for i in range(1,upload["part_count"]+1))
                    if len(data)!=upload["size_bytes"] or sha(data)!=upload["sha256"]:
                        return self.answer(422,{"code":"hash_mismatch"})
                    artifact = {k:upload[k] for k in ("role","sha256","size_bytes","media_type")}
                    upload["state"] = "complete"
                    upload["artifact"] = dict(artifact,artifact_id=ident)
                    server.objects[ident] = data
                    return self.answer(200,upload["artifact"])
                return self.answer(200,upload)
            if path.startswith("work/"):
                return self.work(server, path, payload, token)
            return self.answer(404,{"code":"missing"})

    def work(self, server, path, payload, token):
        work, command = path.split("/")[1:3]
        attempt = server.attempts[work]
        kind = attempt["assignment"]["kind"]
        lease = attempt["assignment"]["lease"]
        if self.headers.get("X-Lease-Id") != lease["lease_id"] or self.headers.get("X-Fence") != str(lease["fence"]):
            return self.answer(409,{"code":"lease_lost"})
        if command=="heartbeat":
            directive = server.stop_work.get(work, "continue")
            if directive != "continue":
                attempt["state"] = {"pause":"paused", "stop":"stopped", "delete":"deleted"}[directive]
            return self.answer(200, dict(directive=directive, server_time=now(),
                                         expires_at=now(85) if directive=="continue" else None))
        if command=="events":
            histories = server.events.setdefault(work,{})
            history = histories.setdefault(lease["attempt"],{})
            for event in payload["events"]:
                seq = event["sequence"]
                if seq in history and history[seq] != event:
                    return self.answer(409, {"code":"event_conflict"})
                if seq not in history and seq != len(history)+1:
                    return self.answer(409, {"code":"event_sequence"})
                history[seq] = event
            return self.answer(200, {"accepted_through":len(history)})
        if command=="uploads":
            server.upload_calls.add(work)
            if work in server.hold_uploads:
                return self.answer(503, {"code":"unavailable", "retryable":True})
            upload_id = f"up-{work}-{lease['attempt']}-{payload['role']}"
            if upload_id not in server.uploads:
                parts = (payload["size_bytes"]+8388607)//8388608
                server.uploads[upload_id] = dict(payload,upload_id=upload_id,work_id=work,state="open",
                                                  part_count=parts,part_size_bytes=8388608,received_parts=[])
                server.parts[upload_id] = {}
            return self.answer(201,server.uploads[upload_id])
        if command=="fail":
            attempt["state"] = "failed"
            attempt["error"] = payload
            return self.answer(200,dict(work_state="failed",attempt=lease["attempt"],retry_after_seconds=None))
        if command=="complete":
            server.commit_calls.setdefault(work, []).append((lease["attempt"], token, payload))
            if work in server.hold_commits:
                return self.answer(503, {"code":"unavailable", "retryable":True})
            roles = set(ROLES[kind])
            if "review_storage" in attempt["assignment"]["input_manifest"]:
                roles.discard("scene_bundle")
            if ({r["role"] for r in payload["outputs"]} != roles or
                    any(server.uploads.get(o["artifact_id"],{}).get("artifact")!=o for o in payload["outputs"])):
                return self.answer(422,{"code":"invalid_manifest"})
            if "receipt" in attempt:
                if attempt["token"] != token or attempt["body"] != payload:
                    return self.answer(409,{"code":"idempotency_conflict"})
                server.replayed[work] = True
                return self.answer(200,attempt["receipt"])
            version = attempt["assignment"]["input_manifest"]["target_version"]
            receipt = dict(receipt_id="receipt-"+work,kind=kind,state="succeeded",version=version,
                           job_id=attempt["assignment"]["job_id"],work_id=work,attempt=lease["attempt"],
                           committed_at=now(),artifacts=payload["outputs"])
            attempt.update(receipt=receipt,body=payload,token=token,state="succeeded")
            server.completed.append(work)
            if attempt["drop_reply"]:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            return self.answer(200,receipt)
        return self.answer(404,{"code":"missing"})


@contextmanager
def mock_service():
    with ProductionMock() as service:
        serving = threading.Thread(target=service.serve_forever)
        serving.start()
        try:
            yield service
        finally:
            service.shutdown()
            serving.join(timeout=10)


@contextmanager
def node_running(config, root, produce):
    with patch("film_worker.scheduler.resolve_tools", return_value={}), patch(
            "film_worker.scheduler.inspect_tools", return_value=VERSIONS), patch.object(Scheduler, "produce", produce):
        node = Scheduler(config, root, "fixture", test_http=True)
        worker = threading.Thread(target=node.run)
        worker.start()
        try:
            yield node
        finally:
            node.stop.set()
            worker.join(timeout=20)
            if worker.is_alive():
                raise AssertionError("worker failed to stop")


def node_config(service, slots=1):
    return {**DEFAULTS, "node_key":"fixture", "server":f"http://127.0.0.1:{service.server_port}",
            "disk_reserve_gib":1, "slots":slots}


production = Scheduler.produce


def fixture_outputs(node, assignment, profile, paths, directory, transfer, progress):
    env = environment(node.cfg, node.versions, profile)
    outputs = {}
    scene_sha = sha(b"fixture-scene")
    for role in ROLES[assignment["kind"]]:
        path = directory/(role+".bin")
        if role == "version_manifest":
            atomic_json(path, dict(frame_count=3, scene_sha256=scene_sha))
        else:
            path.write_bytes((role+assignment["kind"]).encode())
        outputs[role] = str(path)
    result = dict(outputs=outputs, environment=env,
                  automatic_review=dict(scene_sha256=scene_sha, inspected=[1],
                                        outputs_sha256={role: sha(Path(path).read_bytes()) for role, path in outputs.items()}))
    if assignment["kind"] == "finalize":
        result["final_verification"] = dict(frame_count=3, scene_sha256=scene_sha, pts_verified=True, audio_verified=True)
    atomic_json(directory/"result.json", result)
    return production(node, assignment, profile, paths, directory, transfer, progress)


class WorkerFlow(unittest.TestCase):
    def test_local_review_rejects_wrong_instance_and_missing_frozen_version(self):
        with tempfile.TemporaryDirectory(prefix="worker-local-review-") as folder, mock_service() as service, patch(
                "film_worker.scheduler.resolve_tools", return_value={}), patch(
                "film_worker.scheduler.inspect_tools", return_value=VERSIONS):
            owner_data, other_data = Path(folder)/"owner", Path(folder)/"other"
            owner_data.mkdir()
            other_data.mkdir()
            owner = Scheduler(node_config(service), owner_data, "fixture", test_http=True)
            other = Scheduler(node_config(service), other_data, "fixture", test_http=True)
            try:
                storage = {"mode": LOCAL_REVIEW_MODE, "instance_id": owner.instance_id}
                work = service.schedule("prepare", 1, review_storage=storage)
                order = service.attempts[work]["assignment"]
                with self.assertRaisesRegex(WorkFailure, "未派给绑定的执行端"):
                    other.check_assignment(order)
                owner.check_assignment(order)
                with self.assertRaisesRegex(WorkFailure, "本机缺少可核验的基版产物"):
                    owner.local_review_directory(order, sha(b"missing manifest"))
            finally:
                owner.pool.shutdown()
                other.pool.shutdown()

    def test_registration_to_review_and_final_delivery_with_lost_receipt(self):
        with tempfile.TemporaryDirectory(prefix="worker-flow-") as folder, mock_service() as service:
            service.schedule("prepare",1,drop_reply=True)
            with node_running({**node_config(service), "node_key": ""}, Path(folder), fixture_outputs) as node:
                self.wait_for(lambda: service.replayed.get("work-prepare"), node)
                self.wait_for(lambda: node.state.rows()[0]["status"]=="succeeded", node)
                self.assertEqual(len(service.registered),1)
                self.assertNotIn("node_key", service.registered[0])
                self.assertNotIn("profile_hashes", service.registered[0]["capabilities"])
                self.assertNotIn("profile_hash", service.attempts["work-prepare"]["body"])
                self.assertEqual(service.completed,["work-prepare"])
                service.schedule("revise",2)
                self.wait_for(lambda:"work-revise" in service.completed,node)
                service.schedule("finalize",2)
                self.wait_for(lambda:len(node.state.rows())==3 and all(
                    r["status"]=="succeeded" for r in node.state.rows()),node)
                node.reconcile()
                self.assertEqual(node.state.setting("job:job1")["delivery_versions"],[2])
                self.assertEqual(service.completed,["work-prepare","work-revise","work-finalize"])
                self.assertIn("commit_result", {e["stage"] for e in service.events["work-prepare"][1].values()})

    def test_stopping_one_work_does_not_stop_a_sibling_slot(self):
        with tempfile.TemporaryDirectory(prefix="worker-multi-") as folder, mock_service() as service:
            service.heartbeat_interval = 1
            first = service.schedule("prepare",1,slug="first",job="job-a")
            second = service.schedule("prepare",1,slug="second",job="job-b")
            entered = {first:threading.Event(),second:threading.Event()}
            continue_second = threading.Event()

            def pause_fixture(node,assignment,profile,paths,directory,transfer,progress):
                work = assignment["work_id"]
                entered[work].set()
                if work==first:
                    if not transfer.stop.wait(10):
                        raise AssertionError("server command not honored")
                    raise Interrupted
                if not continue_second.wait(10):
                    raise AssertionError("sibling fixture timeout")
                return fixture_outputs(node,assignment,profile,paths,directory,transfer,progress)

            with node_running(node_config(service, slots=2), Path(folder), pause_fixture) as node:
                self.wait_for(lambda: all(event.is_set() for event in entered.values()), node)
                self.assertEqual({a["work_id"] for a in node.summary()["active"]}, {first,second})
                with service.mutex:
                    service.stop_work[first] = "stop"
                self.wait_for(lambda:any(row["work"]==first and row["status"]=="interrupted"
                                         for row in node.state.rows()),node)
                self.assertEqual([a["work_id"] for a in node.summary()["active"]],[second])
                continue_second.set()
                self.wait_for(lambda: service.completed == [second],node)
                self.assertNotIn(first, service.commit_calls)

    def test_restart_resumes_queued_result_and_reuses_same_input_after_reassignment(self):
        for reassign, tamper in ((False,False), (True,False), (True,True)):
            with self.subTest(reassign=reassign, tamper=tamper), tempfile.TemporaryDirectory(prefix="worker-resume-") as folder, mock_service() as service:
                work = service.schedule("prepare",1)
                service.hold_commits.add(work)
                config = node_config(service)
                produced = []

                def track(node,assignment,profile,paths,directory,transfer,progress):
                    produced.append(assignment["lease"]["attempt"])
                    return fixture_outputs(node,assignment,profile,paths,directory,transfer,progress)

                with node_running(config, Path(folder), track) as node:
                    self.wait_for(lambda: bool(service.commit_calls.get(work)),node)
                    first_row = node.state.rows()[0]
                    self.assertIsNotNone(first_row["pending"])
                    self.assertGreater(len(service.uploads),0)

                with service.mutex:
                    service.hold_commits.remove(work)
                if reassign:
                    service.schedule("prepare",1,attempt=2)
                if tamper:
                    file = Path(folder)/"work"/work/"1/scene_bundle.bin"
                    file.write_bytes(b"!"*file.stat().st_size)
                with node_running(config, Path(folder), track) as node:
                    attempt = 2 if reassign else 1
                    self.wait_for(lambda:any(r["number"]==attempt and r["status"]=="succeeded"
                                             for r in node.state.rows()),node)
                    self.assertEqual(produced,[1,2] if tamper else [1])
                    calls = service.commit_calls[work]
                    self.assertEqual(calls[-1][0],attempt)
                    if not reassign:
                        self.assertEqual(calls[0],calls[-1])
                    else:
                        self.assertNotEqual(calls[0][1],calls[-1][1])
                        self.assertEqual([a["sha256"] for a in calls[0][2]["outputs"]],
                                         [a["sha256"] for a in calls[-1][2]["outputs"]])

    def test_server_stop_during_first_confirmation_prevents_production(self):
        with tempfile.TemporaryDirectory(prefix="worker-fenced-") as folder, mock_service() as service:
            work = service.schedule("prepare",1)
            service.stop_work[work] = "stop"

            def unexpected(*_):
                self.fail("production started before lease confirmation")

            with node_running(node_config(service),Path(folder),unexpected) as node:
                self.wait_for(lambda: any(row["status"]=="interrupted" for row in node.state.rows()),node)
                self.assertNotIn(work,service.upload_calls)
                self.assertNotIn(work,service.commit_calls)

    def test_corrupt_result_on_restart_is_regenerated_before_upload(self):
        with tempfile.TemporaryDirectory(prefix="worker-corrupt-") as folder, mock_service() as service:
            root = Path(folder)
            work = service.schedule("prepare",1)
            service.hold_uploads.add(work)
            config = node_config(service)
            with node_running(config,root,fixture_outputs) as node:
                self.wait_for(lambda:work in service.upload_calls,node)
                self.assertIsNone(node.state.rows()[0]["pending"])
            file = root/"work"/work/"1/scene_bundle.bin"
            file.write_bytes(b"!"*file.stat().st_size)
            with service.mutex:
                service.hold_uploads.remove(work)

            def recreate(node,assignment,profile,paths,directory,transfer,progress):
                self.assertFalse((directory/"result.json").exists())
                return fixture_outputs(node,assignment,profile,paths,directory,transfer,progress)

            with node_running(config,root,recreate) as node:
                self.wait_for(lambda:service.completed == [work],node)
                submitted = next(a for a in service.commit_calls[work][0][2]["outputs"] if a["role"]=="scene_bundle")
                self.assertEqual(submitted["sha256"], sha(b"scene_bundleprepare"))

    def test_deleted_job_cleans_all_work_without_removing_shared_input(self):
        with tempfile.TemporaryDirectory(prefix="worker-delete-") as folder, mock_service() as service:
            root = Path(folder)
            first = service.schedule("prepare",1,slug="a-first",job="job-a")
            revised = service.schedule("revise",2,slug="a-revise",job="job-a")
            other = service.schedule("prepare",1,slug="b-first",job="job-b")
            with node_running(node_config(service),root,fixture_outputs) as node:
                self.wait_for(lambda:len(node.state.rows())==3 and all(
                    row["status"]=="succeeded" for row in node.state.rows()),node)
                shared = root/"cache"/"sha256"/sha(service.input)
                self.assertTrue(shared.is_file())
                with service.mutex:
                    service.attempts[first]["state"] = "deleted"
                    service.attempts[revised]["state"] = "deleted"
                node.staleness.clear()
                node.reconcile()
                self.assertFalse((root/"work"/first).exists())
                self.assertFalse((root/"work"/revised).exists())
                self.assertTrue((root/"work"/other).exists())
                self.assertTrue(shared.is_file())
                with service.mutex:
                    service.attempts[other]["state"] = "deleted"
                node.staleness.pop("job-b",None)
                node.reconcile()
                self.assertFalse(shared.exists())

    def wait_for(self, condition, node):
        limit=time.monotonic()+12
        while time.monotonic()<limit:
            if condition():
                return
            time.sleep(0.05)
        self.fail(str(node.summary()))
