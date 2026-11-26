import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from film_worker import media, oss_delivery
from film_worker.protocol import OSS_OBJECTS, Client, ReviewFrameLimitError, Transfer, digest
from film_worker.scheduler import Scheduler, attempt_history, output_roles
from film_worker.settings import atomic_json
from film_worker.state import State


SOURCE_URL = "https://review-bucket.oss-cn-hangzhou.aliyuncs.com/movie-review/published/source.mp4"


def assignment(mode):
    return dict(job_id="9", work_id="work_test", kind="revise", input_manifest_sha256="a" * 64,
                input_manifest={"review_storage": {"mode": "node_local_v1"}, "review_upload": "rejected_frames_v1"},
                lease=dict(attempt=1, fence=1, lease_id="lease_test"),
                oss=dict(source_url=SOURCE_URL, delivery=mode, upload=None if mode == "manual" else
                         dict(region="cn-hangzhou", bucket="review-bucket",
                              key="movie-review/incoming/work/9/work_test/1/bundle_test.zip")))


class OSSDeliveryTests(unittest.TestCase):
    def test_frame_limit_rejects_new_and_cached_timelines_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            measurement = ({}, {}, list(range(2401)), None, None, None)
            with patch.object(media.intake, "measure", return_value=measurement), patch.object(media, "ffmpeg") as ffmpeg:
                with self.assertRaisesRegex(ReviewFrameLimitError, "2,401.*2,400"):
                    media.prepare_source(root, source, 1920)
                ffmpeg.assert_not_called()
                timeline = root / "intake" / "timeline.json"
                atomic_json(timeline, {"pts": list(range(2400))})
                self.assertEqual(len(media.prepare_source(root, source, 1920)["pts"]), 2400)
                atomic_json(timeline, {"pts": list(range(2401))})
                with self.assertRaises(ReviewFrameLimitError):
                    media.prepare_source(root, source, 1920)
                ffmpeg.assert_not_called()

    def test_local_rejection_reports_supported_failure_and_skips_oss(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = assignment("manual")
            directory = root / "work" / work["work_id"] / "1"
            version = directory / "version.json"
            atomic_json(version, {"frame_count": 1})
            checkpoint = dict(attempt=1, outputs={"version_manifest": dict(name=version.name, sha256=digest(version),
                                                                           size=version.stat().st_size)})
            binding = dict(job_id=work["job_id"], work_id=work["work_id"], attempt=1,
                           outputs_sha256={"version_manifest": digest(version)})
            worker = Scheduler.__new__(Scheduler)
            worker.root, worker.state = root, State(root)
            worker.api = Mock()
            worker.check_assignment = Mock(return_value=({}, {}))
            worker.load_checkpoint = Mock(return_value=checkpoint)
            worker.state.start(work)
            atomic_json(directory / "local-review.json", binding)
            atomic_json(worker.local_approval_path(work["work_id"], 1), dict(binding, decision="reject"))
            worker.api.json.return_value = ({"work_state": "failed"}, None)
            current = {"directory": directory, "stop": Event(), "progress": {}}
            with (patch("film_worker.scheduler.LeaseWatch"), patch("film_worker.scheduler.Events"),
                  patch.object(oss_delivery, "submission") as submission):
                worker.execute_attempt(work, 100, 20, current, None)
            submission.assert_not_called()
            endpoint = worker.api.json.call_args
            self.assertEqual(endpoint.args, ("POST", "/work/work_test/fail"))
            self.assertEqual(endpoint.kwargs["body"]["code"], "other")
            self.assertIn("本地复核未通过", worker.state.rows()[0]["message"])
            self.assertEqual(worker.state.rows()[0]["status"], "failed")

    def test_source_and_redirect_resume_without_manager_credentials(self):
        content = b"video bytes"
        client = Client("https://manager.local", "private-worker-token")
        exit_headers = Message()
        exit_headers["Location"] = SOURCE_URL + "?signed=value"
        for mode in ("fresh", "redirect", "expired"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                item = dict(sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), artifact_id="source")
                (folder / (item["sha256"] + ".part")).write_bytes(content[:2])
                response = io.BytesIO(content[2:])
                response.status = 206
                response.headers = {"Content-Range": f"bytes 2-{len(content)-1}/{len(content)}", "ETag": '"oss-etag"'}
                client.opener = Mock()
                client.opener.open.side_effect = HTTPError("http://manager.local", 302, "", exit_headers, io.BytesIO())
                client.oss_opener = Mock()
                client.oss_opener.open.return_value = response
                if mode == "expired":
                    expired = HTTPError(SOURCE_URL, 403, "", Message(), io.BytesIO())
                    client.oss_opener.open.side_effect = [expired, response]
                transfer = Transfer(client, assignment("manual"), Event(), folder / "cache")
                path = transfer.download(item, folder, source_url=None if mode == "redirect" else SOURCE_URL)
                self.assertEqual(path.read_bytes(), content)
                for call in client.oss_opener.open.call_args_list:
                    self.assertEqual(dict(call.args[0].header_items()), {"Range": "bytes=2-"})
                if mode == "fresh":
                    client.opener.open.assert_not_called()
                else:
                    manager_request = client.opener.open.call_args.args[0]
                    self.assertEqual(manager_request.get_header("Authorization"), "Bearer private-worker-token")
                    self.assertEqual(manager_request.get_header("X-lease-id"), "lease_test")
                    self.assertEqual(client.oss_opener.open.call_args.args[0].full_url, SOURCE_URL + "?signed=value")
                    self.assertEqual(client.oss_opener.open.call_count, 2 if mode == "expired" else 1)

    def test_download_rejects_bad_hash_and_untrusted_destination(self):
        client = Client("https://manager.local", "token")
        with self.assertRaisesRegex(ValueError, "OSS"):
            client.open_oss("https://review-bucket.oss-cn-hangzhou.aliyuncs.com.other.invalid/path", 0)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            transfer = Transfer(client, assignment("manual"), Event(), folder / "cache")
            response = io.BytesIO(b"bad")
            response.status = 200
            response.headers = {}
            with patch.object(client, "open_oss", return_value=response):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    transfer.download(dict(sha256="0" * 64, size_bytes=3, artifact_id="source"),
                                      folder, source_url=SOURCE_URL)
            self.assertFalse((folder / "cache" / ("0" * 64)).exists())

    def test_auto_and_manual_submit_wait_for_final_receipt(self):
        for mode in ("auto", "manual"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work = assignment(mode)
                oss_delivery.validate_assignment(work)
                directory = root / "work" / work["work_id"] / "1"
                out = directory / "out"
                out.mkdir(parents=True)
                checkpoint = dict(attempt=1, outputs={}, environment={})
                for role in output_roles(work):
                    path = out / (role + (".zip" if role == "review_bundle" else ".json"))
                    if role == "review_bundle":
                        with zipfile.ZipFile(path, "w") as inner:
                            inner.writestr("pairs/0002.jpg", b"changed frame")
                    elif role == "version_manifest":
                        path.write_text('{"frame_count":3}', encoding="utf-8")
                    else:
                        path.write_text("{}", encoding="utf-8")
                    checkpoint["outputs"][role] = dict(name=str(path.relative_to(directory)), sha256=digest(path),
                                                        size=path.stat().st_size)

                worker = Scheduler.__new__(Scheduler)
                worker.root, worker.state = root, State(root)
                worker.api = Mock()
                worker.cfg = {"transfer_slots": 1}
                worker.delivery_checks = {}
                worker.check_assignment = Mock(return_value=({}, {}))
                worker.load_checkpoint = Mock(return_value=checkpoint)
                worker.await_local_review = Mock()
                worker.has_local_review = Mock(return_value=True)
                worker.state.start(work)
                expected = "verifying" if mode == "auto" else "awaiting_manual"
                worker.api.json.return_value = (dict(state=expected), None)
                current = {"directory": directory, "stop": Event(), "progress": {}}
                with (patch("film_worker.scheduler.LeaseWatch"), patch("film_worker.scheduler.Events"),
                      patch("film_worker.scheduler.read_oss_keys", return_value=("testId", "testSecret")),
                      patch.object(oss_delivery, "upload") as upload):
                    worker.execute_attempt(work, 100, 20, current, None)

                endpoint = worker.api.json.call_args
                self.assertEqual(endpoint.args, ("POST", "/work/work_test/oss/submit"))
                self.assertEqual(worker.state.rows()[0]["status"], expected)
                self.assertIsNone(worker.state.rows()[0]["receipt"])
                archive = directory / "submission.zip"
                if mode == "auto":
                    upload.assert_called_once()
                    self.assertEqual(upload.call_args.args[:3], (archive, work["oss"]["upload"], ("testId", "testSecret")))
                    self.assertEqual(endpoint.kwargs["body"], dict(input_manifest_sha256="a" * 64,
                                                                   size_bytes=archive.stat().st_size, sha256=digest(archive)))
                else:
                    upload.assert_not_called()
                    self.assertEqual(endpoint.kwargs["body"], dict(input_manifest_sha256="a" * 64))
                    self.assertEqual(attempt_history(root, worker.state)[0]["submission_path"], str(archive))
                with zipfile.ZipFile(archive) as bundle:
                    data = json.loads(bundle.read("submission.json"))
                    self.assertEqual(data["attempt"], 1)
                    self.assertEqual(set(bundle.namelist()), {"submission.json", *data["outputs"].values()})
                    with zipfile.ZipFile(io.BytesIO(bundle.read(data["outputs"]["review_bundle"]))) as inner:
                        self.assertEqual(inner.namelist(), ["pairs/0002.jpg"])

                worker.work_status = Mock(return_value=dict(attempt=1, work_state="succeeded", job_state="review",
                                                            receipt={"attempt": 1, "receipt_id": "receipt_test"}))
                worker.poll_deliveries()
                self.assertEqual(worker.state.rows()[0]["status"], "succeeded")

                worker.state.mark(work, status="verifying")
                worker.delivery_checks.clear()
                worker.work_status.return_value = dict(attempt=1, work_state="awaiting_manual" if mode == "manual"
                                                       else "failed", job_state="review", failure="integrity_failed")
                worker.poll_deliveries()
                row = worker.state.rows()[0]
                self.assertEqual(row["status"], worker.work_status.return_value["work_state"])
                self.assertIn("247" if mode == "manual" else "integrity_failed", row["message"])

    def test_upload_uses_tls_v4_and_sdk_checkpoints(self):
        import alibabacloud_oss_v2 as oss

        with tempfile.TemporaryDirectory() as temp, patch.object(oss, "Client") as client:
            path = Path(temp) / "submission.zip"
            path.write_bytes(b"zip")
            uploader = client.return_value.uploader.return_value
            uploader.upload_file.return_value = SimpleNamespace(status_code=200)
            target = assignment("auto")["oss"]["upload"]
            oss_delivery.upload(path, target, ("testId", "testSecret"), Event(), Mock())
            config = client.call_args.args[0]
            self.assertEqual((config.endpoint, config.signature_version), ("https://oss-cn-hangzhou.aliyuncs.com", "v4"))
            self.assertTrue(client.return_value.uploader.call_args.kwargs["enable_checkpoint"])
            request = uploader.upload_file.call_args.args[0]
            self.assertEqual((request.bucket, request.key), (target["bucket"], target["key"]))

    def test_direct_final_delivery_waits_for_receipt_and_reuses_object_version(self):
        for mode in ("auto", "manual"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work = assignment(mode)
                work["kind"] = "finalize"
                work["input_manifest"] = {"review_storage": {"mode": "node_local_v1"}, "target_version": 2}
                work["oss"].update(protocol=OSS_OBJECTS, review_scope=None, upload=None)
                oss_delivery.validate_assignment(work)
                directory = root / "work" / work["work_id"] / "1"
                out = directory / "out"
                out.mkdir(parents=True)
                proof = dict(frame_count=1, pts_verified=True, audio_verified=True)
                checkpoint = dict(attempt=1, outputs={}, environment={}, final_verification=proof)
                for role in output_roles(work):
                    path = out / ("delivery.zip" if role == "delivery_bundle" else role + ".mp4")
                    if role == "delivery_bundle":
                        archive = path
                        with zipfile.ZipFile(archive, "w") as bundle:
                            bundle.writestr("delivery-proof.json", json.dumps(dict(
                                input_manifest_sha256=work["input_manifest_sha256"], final_verification=proof)))
                    else:
                        path.write_bytes(b"dummy video")
                    checkpoint["outputs"][role] = dict(name=str(path.relative_to(directory)), sha256=digest(path),
                                                        size=path.stat().st_size)
                target = dict(region="cn-hangzhou", bucket="review-bucket", upload_id="upload_test",
                              key="movie-review/jobs/9/delivery/from-v0002/work_test/a0001/upload_test")
                worker = Scheduler.__new__(Scheduler)
                worker.root, worker.state = root, State(root)
                worker.api = Mock()
                worker.cfg = {"transfer_slots": 1}
                worker.delivery_checks = {}
                worker.check_assignment = Mock(return_value=({}, {}))
                worker.load_checkpoint = Mock(return_value=checkpoint)
                worker.await_local_review = Mock()
                worker.state.start(work)
                expected = "verifying" if mode == "auto" else "awaiting_manual"
                worker.api.json.side_effect = lambda method, path, **kw: (
                    target if path.endswith("/objects") else dict(state=expected), None)
                sdk = Mock()
                sdk.upload_file.return_value = SimpleNamespace(status_code=200, version_id="pinned-version")
                current = {"directory": directory, "stop": Event(), "progress": {}}
                with (patch("film_worker.scheduler.LeaseWatch"), patch("film_worker.scheduler.Events"),
                      patch("film_worker.scheduler.read_oss_keys", return_value=("testId", "testSecret")),
                      patch.object(oss_delivery, "make_uploader", return_value=sdk)):
                    worker.execute_attempt(work, 100, 20, current, None)
                    self.assertEqual(worker.state.rows()[0]["status"], expected)
                    self.assertIsNone(worker.state.rows()[0]["receipt"])
                    request = worker.api.json.call_args
                    self.assertEqual(request.args, ("POST", "/work/work_test/oss/submit"))
                    body = {"input_manifest_sha256": work["input_manifest_sha256"]}
                    if mode == "auto":
                        upload = sdk.upload_file.call_args.args[0]
                        self.assertEqual(upload.key, target["key"] + "/delivery.zip")
                        self.assertEqual(upload.content_type, "application/zip")
                        self.assertEqual(upload.metadata, {"sha256": digest(archive)})
                        body.update(upload_id=target["upload_id"], final_verification=proof, objects={"delivery.zip": dict(
                            size_bytes=archive.stat().st_size, sha256=digest(archive), version_id="pinned-version")})
                        transfer = Transfer(worker.api, work, Event(), root / "cache")
                        self.assertEqual(worker.upload_result(work, {}, checkpoint, directory, transfer), body)
                        sdk.upload_file.assert_called_once()
                        worker.state.mark(work, status=expected)
                    else:
                        manual = directory / "delivery.zip"
                        self.assertEqual(archive.read_bytes(), manual.read_bytes())
                        self.assertEqual(attempt_history(root, worker.state)[0]["submission_path"], str(manual))
                        sdk.upload_file.assert_not_called()
                    self.assertEqual(request.kwargs["body"], body)
                worker.work_status = Mock(return_value=dict(attempt=1, work_state="succeeded", job_state="completed",
                                                            receipt={"attempt": 1, "receipt_id": "receipt_test"}))
                worker.poll_deliveries()
                self.assertEqual(worker.state.rows()[0]["status"], "succeeded")

    def test_upload_only_retries_transient_sdk_errors(self):
        import alibabacloud_oss_v2 as oss
        from alibabacloud_oss_v2 import exceptions
        from alibabacloud_oss_v2.uploader import UploadError

        target = assignment("auto")["oss"]["upload"]
        with tempfile.TemporaryDirectory() as temp, patch.object(oss, "Client") as client:
            path = Path(temp) / "submission.zip"
            path.write_bytes(b"zip")
            upload = client.return_value.uploader.return_value.upload_file
            stopped = Event()
            failure = exceptions.RequestError(error=TimeoutError("private request information"))
            upload.side_effect = [UploadError(upload_id="upload", path="private URL", error=failure),
                                  SimpleNamespace(status_code=200)]
            with patch.object(stopped, "wait", return_value=False):
                oss_delivery.upload(path, target, ("testId", "testSecret"), stopped, Mock())
            self.assertEqual(upload.call_count, 2)
            self.assertEqual(upload.call_args_list[0], upload.call_args_list[1])
            upload.reset_mock(side_effect=True)
            upload.side_effect = UploadError(upload_id="upload", path="private URL", error=exceptions.ServiceError(
                status_code=403, code="AccessDenied", request_id="test", message="private request information",
                ec="", timestamp="", request_target="private URL"))
            with self.assertRaisesRegex(oss_delivery.UploadFailure, r"^OSS 交付包上传失败（AccessDenied）$") as error:
                oss_delivery.upload(path, target, ("testId", "testSecret"), stopped, Mock())
            self.assertFalse(error.exception.retryable)
            upload.assert_called_once()

    def test_final_delivery_zip_is_bound_to_the_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = assignment("manual")
            work["kind"] = "finalize"
            work["lease"]["attempt"] = 2
            artifacts = {}
            checkpoint = {"outputs": {}, "final_verification": {"frame_count": 130, "pts_verified": True}}
            for role in ("white_video", "comparison_video", "delivery_bundle"):
                path = root / (role + ".bin")
                path.write_bytes(role.encode())
                artifacts[role] = path
                checkpoint["outputs"][role] = dict(size=path.stat().st_size, sha256=digest(path))
            archive, metadata = oss_delivery.submission(work, checkpoint, root, artifacts, Event(), Mock())
            with zipfile.ZipFile(archive) as bundle:
                header = json.loads(bundle.read("submission.json"))
            self.assertEqual(header["attempt"], 2)
            self.assertEqual(header["final_verification"], checkpoint["final_verification"])
            self.assertEqual(set(header["outputs"]), set(artifacts))
            self.assertEqual(metadata["sha256"], digest(archive))
