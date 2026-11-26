"""Prepare and upload the work order's Alibaba Cloud OSS objects."""
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .bundles import safe_path
from .protocol import Interrupted, LOCAL_REVIEW_MODE, OSS_BUCKET, OSS_OBJECTS, OSS_REGION, digest, opaque, oss_url
from .settings import atomic_json


MAX_BYTES = 16 * 1024**3
ZIP_NAME = "submission.zip"
DELIVERY_STATES = ("awaiting_manual", "verifying")
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
VERSION_ID = re.compile(r"[A-Za-z0-9+/=._~-]{1,256}\Z")
MAX_JPEG = 14 * 1024**2
MAX_METADATA = 16 * 1024**2
REVIEW_FILES = {"review-index.json": ("review_index", "application/json"),
                "version.json": ("version_manifest", "application/json"),
                "production.json": ("production_report", "application/json"),
                "meta.jsonl": ("shot_metadata", "application/x-ndjson"),
                "timeline.json": ("source_timeline", "application/json")}


@dataclass(frozen=True)
class ObjectFile:
    path: Path
    size: int
    sha256: str
    content_type: str

    def declaration(self):
        return dict(size_bytes=self.size, sha256=self.sha256)


def uses_objects(assignment):
    return assignment.get("oss", {}).get("protocol") == OSS_OBJECTS


def manual_path(directory, assignment):
    return directory / ("delivery.zip" if uses_objects(assignment) and assignment["kind"] == "finalize" else ZIP_NAME)


class UploadFailure(RuntimeError):
    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


def validate_assignment(assignment):
    cloud = assignment["oss"]
    oss_url(cloud["source_url"])
    mode, upload = cloud["delivery"], cloud["upload"]
    if uses_objects(assignment):
        scope = {"prepare": "all_frames", "revise": "rejected_frames", "finalize": None}[assignment["kind"]]
        storage = assignment["input_manifest"].get("review_storage") or {}
        if (mode not in {"auto", "manual"} or upload is not None or cloud.get("review_scope") != scope
                or storage.get("mode") != LOCAL_REVIEW_MODE):
            raise ValueError("OSS 直传工单的交付方式、审片范围或实例绑定无效")
        return
    if cloud.get("protocol") not in {None, "zip_v1"}:
        raise ValueError("未知的 OSS 交付协议")
    if mode == "manual" and upload is None:
        return
    if mode != "auto" or not isinstance(upload, dict):
        raise ValueError("OSS 工单交付方式无效")
    prefix = f"movie-review/incoming/work/{opaque(assignment['job_id'])}/{opaque(assignment['work_id'])}/{assignment['lease']['attempt']}/"
    if (not OSS_REGION.fullmatch(upload["region"]) or not OSS_BUCKET.fullmatch(upload["bucket"])
            or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+\.zip", upload["key"])):
        raise ValueError("OSS 上传位置与领取批次不匹配")


def begin_objects(assignment, payload, transfer):
    target = transfer.json("POST", f"/work/{transfer.work}/oss/objects", payload, "oss-objects")
    upload = opaque(target["upload_id"])
    order = assignment["input_manifest"]
    version = order["target_version"]
    stage = f"delivery/from-v{version:04d}" if assignment["kind"] == "finalize" else f"review/v{version:04d}"
    work, job, attempt = opaque(assignment["work_id"]), opaque(assignment["job_id"]), assignment["lease"]["attempt"]
    if (not OSS_REGION.fullmatch(target["region"]) or not OSS_BUCKET.fullmatch(target["bucket"])
            or target["key"] != f"movie-review/jobs/{job}/{stage}/{work}/a{attempt:04d}/{upload}"):
        raise ValueError("OSS 对象目录与当前领取批次不匹配")
    return target


def object_files(assignment, output, paths, stopped):
    from PIL import Image

    def checked(path, size, sha, mime, limit):
        if stopped.is_set():
            raise Interrupted
        if not 0 < size <= limit or path.stat().st_size != size or digest(path) != sha:
            raise ValueError(f"OSS 产物 {path.name} 的大小或摘要与清单不符")
        return ObjectFile(path, size, sha, mime)

    def artifact(role, mime, limit):
        info = output[role]
        return checked(paths[role], info["size"], info["sha256"], mime, limit)

    if assignment["kind"] == "finalize":
        return {"delivery.zip": artifact("delivery_bundle", "application/zip", MAX_BYTES)}

    files = {name: artifact(role, mime, MAX_METADATA) for name, (role, mime) in REVIEW_FILES.items()
             if name != "timeline.json" or assignment["kind"] == "prepare"}
    index = json.loads(paths["review_index"].read_text(encoding="utf-8"))
    version = json.loads(paths["version_manifest"].read_text(encoding="utf-8"))
    if (version["review_index_sha256"] != output["review_index"]["sha256"]
            or index["input_manifest_sha256"] != assignment["input_manifest_sha256"]):
        raise ValueError("OSS 审核索引与本轮产物不一致")
    rows = index["frames"]
    numbers = [row["number"] for row in rows]
    count = version["frame_count"]
    if (not numbers or numbers != sorted(set(numbers)) or any(type(n) is not int or not 1 <= n <= count for n in numbers)
            or (assignment["kind"] == "prepare" and numbers != list(range(1, count + 1)))):
        raise ValueError("OSS 审核索引未覆盖要求的帧")
    review_root = paths["review_bundle"].parent.parent / "review-bundle"
    width, height = (version["review_size"][k] for k in ("width", "height"))
    for row in rows:
        name = f"pairs/{row['number']:06d}.jpg"
        path = review_root / name
        files[name] = checked(path, row["size_bytes"], row["sha256"], "image/jpeg", MAX_JPEG)
        with Image.open(path, formats=["JPEG"]) as image:
            if image.size != (width, 2 * height):
                raise ValueError(f"OSS 审核对比图 {path.name} 画幅不符")
            image.load()
    return files


def package_manual_objects(assignment, directory, files, final_verification, stopped, progress):
    archive = manual_path(directory, assignment)
    partial = archive.with_suffix(".partial")
    try:
        if assignment["kind"] == "finalize":
            item = files["delivery.zip"]
            with zipfile.ZipFile(item.path) as bundle:
                proof = bundle.getinfo("delivery-proof.json")
                if (proof.file_size > 1024**2 or json.loads(bundle.read(proof)) != dict(
                        input_manifest_sha256=assignment["input_manifest_sha256"], final_verification=final_verification)):
                    raise ValueError("手动交付凭证与当前工单不符")
            partial.unlink(missing_ok=True)
            try:
                os.link(item.path, partial)
            except OSError:
                with item.path.open("rb") as source, partial.open("wb") as target:
                    copy_object(source, target, item, stopped)
        else:
            total = sum(item.size for item in files.values())
            if total > MAX_BYTES:
                raise ValueError("手动审核 ZIP 超过 16 GiB")
            progress("package_oss", 0, total)
            done = 0
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for name, item in files.items():
                    with item.path.open("rb") as source, bundle.open(name, "w", force_zip64=True) as target:
                        copy_object(source, target, item, stopped)
                    done += item.size
                    if done == total or done // (32 * 1024**2) != (done - item.size) // (32 * 1024**2):
                        progress("package_oss", done, total)
        if stopped.is_set():
            raise Interrupted
        if partial.stat().st_size > MAX_BYTES:
            raise ValueError("手动交付 ZIP 超过 16 GiB")
        partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)


def copy_object(source, target, item, stopped):
    checksum = hashlib.sha256()
    size = 0
    while block := source.read(4 * 1024**2):
        if stopped.is_set():
            raise Interrupted
        target.write(block)
        checksum.update(block)
        size += len(block)
    if size != item.size or checksum.hexdigest() != item.sha256:
        raise ValueError("打包时 OSS 产物已变化")


def submission(assignment, checkpoint, directory, artifacts, stopped, progress):
    header = {field: assignment[field] for field in ("job_id", "work_id", "input_manifest_sha256")}
    header["attempt"] = assignment["lease"]["attempt"]
    header["outputs"] = {role: str(safe_path(f"out/{role}{path.suffix}")) for role, path in artifacts.items()}
    if assignment["kind"] == "finalize":
        header["final_verification"] = checkpoint["final_verification"]

    outputs = checkpoint["outputs"]
    archive = directory / ZIP_NAME
    record_path = directory / "submission-info.json"
    identity = dict(header=header, outputs=outputs)
    if stopped.is_set():
        raise Interrupted
    try:
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        if (cached["identity"] == identity and cached["size_bytes"] == archive.stat().st_size
                and cached["sha256"] == digest(archive)):
            return archive, {field: cached[field] for field in ("size_bytes", "sha256")}
    except (OSError, ValueError, KeyError, TypeError):
        pass

    manifest = json.dumps(header, ensure_ascii=False, allow_nan=False).encode("utf-8")
    total = sum(item["size"] for item in outputs.values()) + len(manifest)
    if total > MAX_BYTES:
        raise ValueError("交付包展开大小超过 16 GiB")
    progress("package_oss", 0, total)
    partial = archive.with_suffix(".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            bundle.writestr("submission.json", manifest)
            done = len(manifest)
            last_update = time.monotonic()
            for role, name in header["outputs"].items():
                checksum, size = hashlib.sha256(), 0
                with artifacts[role].open("rb") as source, bundle.open(name, "w", force_zip64=True) as target:
                    while block := source.read(4 * 1024**2):
                        if stopped.is_set():
                            raise Interrupted
                        target.write(block)
                        checksum.update(block)
                        size += len(block)
                        now = time.monotonic()
                        if now - last_update >= 0.5:
                            progress("package_oss", done + size, total)
                            last_update = now
                if size != outputs[role]["size"] or checksum.hexdigest() != outputs[role]["sha256"]:
                    raise ValueError("打包时产物与审核通过的摘要不一致")
                done += size
        size = partial.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("交付 ZIP 大小超过 16 GiB")
        if stopped.is_set():
            raise Interrupted
        payload = dict(size_bytes=size, sha256=digest(partial))
        partial.replace(archive)
        atomic_json(record_path, dict(identity=identity, **payload))
        progress("package_oss", total, total)
        return archive, payload
    finally:
        partial.unlink(missing_ok=True)


def make_uploader(target, credentials, checkpoint_dir):
    import alibabacloud_oss_v2 as oss

    config = oss.config.load_default()
    config.region = target["region"]
    config.endpoint = f"https://oss-{config.region}.aliyuncs.com"
    config.signature_version = "v4"
    config.credentials_provider = oss.credentials.StaticCredentialsProvider(*credentials)
    config.connect_timeout, config.readwrite_timeout = 8, 25
    config.retry_max_attempts = 4
    client = oss.Client(config)
    return client.uploader(part_size=8 * 1024**2, parallel_num=2, enable_checkpoint=True,
                           leave_parts_on_error=True, checkpoint_dir=str(checkpoint_dir))


def upload_objects(target, credentials, files, directory, stopped, progress):
    journal = directory / "oss-objects" / opaque(target["upload_id"])
    journal.mkdir(parents=True, exist_ok=True)
    uploader = make_uploader(target, credentials, journal)
    total = sum(item.size for item in files.values())
    completed, last_update = 0, 0.0
    progress("oss_upload", 0, total)

    def report(_, done, __):
        nonlocal last_update
        now = time.monotonic()
        if now - last_update >= 0.5:
            progress("oss_upload", completed + done, total)
            last_update = now

    declarations = {}
    for name, item in files.items():
        if stopped.is_set():
            raise Interrupted
        key = f"{target['key']}/{safe_path(name)}"
        record = journal / safe_path(name + ".json")
        expected = dict(bucket=target["bucket"], region=target["region"], key=key, **item.declaration())
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
            version = saved.get("version_id")
            if not all(saved.get(k) == v for k, v in expected.items()) or not valid_version(version):
                version = None
        except (OSError, ValueError, AttributeError):
            version = None
        if version is None:
            version = upload(item.path, dict(target, key=key), credentials, stopped, report,
                             content_type=item.content_type, sha256=item.sha256, uploader=uploader, size=item.size)
            atomic_json(record, dict(expected, version_id=version))
        declarations[name] = dict(item.declaration(), version_id=version)
        completed += item.size
        report("oss_upload", 0, item.size)
    progress("oss_upload", total, total)
    return declarations


def valid_version(value):
    return isinstance(value, str) and value != "null" and VERSION_ID.fullmatch(value) is not None


def upload(archive, target, credentials, stopped, progress, *, content_type="application/zip",
           sha256=None, uploader=None, size=None):
    import alibabacloud_oss_v2 as oss
    from alibabacloud_oss_v2 import exceptions
    from alibabacloud_oss_v2.uploader import UploadError

    if stopped.is_set():
        raise Interrupted
    uploader = uploader if uploader is not None else make_uploader(target, credentials, archive.parent)
    last_update = 0.0

    def report(_, done, total):
        nonlocal last_update
        if stopped.is_set():
            raise Interrupted
        now = time.monotonic()
        if now - last_update >= 0.5 or done == total:
            progress("oss_upload", done, total)
            last_update = now

    actual_size = archive.stat().st_size
    if size is not None and (actual_size != size or digest(archive) != sha256):
        raise ValueError("上传前 OSS 产物已变化")
    size = actual_size
    progress("oss_upload", 0, size)
    request = oss.PutObjectRequest(bucket=target["bucket"], key=target["key"], content_type=content_type,
                                   metadata={"sha256": sha256} if sha256 else None, progress_fn=report)
    for attempt in range(3):
        if stopped.is_set():
            raise Interrupted
        try:
            result = uploader.upload_file(request, filepath=str(archive))
        except (exceptions.BaseError, OSError) as exc:
            cause = exc
            while isinstance(cause, (UploadError, exceptions.OperationError)) and cause.unwrap() is not None:
                cause = cause.unwrap()
            status = getattr(cause, "status_code", 0)
            retryable = (isinstance(cause, (exceptions.RequestError, exceptions.ResponseError, TimeoutError, ConnectionError))
                         or status in RETRYABLE_STATUS)
            # SDK errors may include signed URLs. Only report the public error code or status.
            code = getattr(cause, "code", "")
            detail = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code) else (
                f"HTTP {status}" if status else "")
        else:
            status = result.status_code
            if 200 <= status < 300:
                if stopped.is_set():
                    raise Interrupted
                version = getattr(result, "version_id", None)
                if sha256 and not valid_version(version):
                    raise UploadFailure("OSS 审核桶未返回对象版本 ID，请检查 Bucket 版本控制", retryable=False)
                progress("oss_upload", size, size)
                return version
            retryable, detail = status in RETRYABLE_STATUS, f"HTTP {status}"
        if not retryable or attempt == 2:
            if stopped.is_set():
                raise Interrupted
            suffix = f"（{detail}）" if detail else ""
            raise UploadFailure(f"OSS 交付包上传失败{suffix}", retryable=retryable)
        if stopped.wait(2 ** attempt):
            raise Interrupted
