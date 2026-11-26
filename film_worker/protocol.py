"""Outbound worker protocol and durable transfers."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


REVIEW_STORAGE_HEADER = "X-Film-Worker-Review-Storage"
LOCAL_REVIEW_MODE = "node_local_v1"
REVIEW_UPLOAD_HEADER = "X-Film-Worker-Review-Upload"
REJECTED_FRAMES_MODE = "rejected_frames_v1"
MAX_REVIEW_FRAMES = 2400
OSS_OBJECTS = "objects_v1"
OSS_REGION = re.compile(r"cn-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
OSS_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")


def oss_url(url):
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("OSS 下载地址无效")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    match = re.fullmatch(r"([^.]*)\.oss-(cn-[a-z0-9-]+)\.aliyuncs\.com", host)
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.fragment or not match
            or not OSS_BUCKET.fullmatch(match[1]) or not OSS_REGION.fullmatch(match[2])):
        raise ValueError("OSS 下载地址必须是阿里云 OSS 的 HTTPS 地址")
    return url


class ProtocolError(Exception):
    def __init__(self, status, code, retryable=False, retry_after=None):
        super().__init__(f"HTTP {status}: {code}")
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class Interrupted(Exception):
    """The current process no longer has permission to continue the work."""


class ReviewFrameLimitError(ValueError):
    def __init__(self, count):
        super().__init__(f"原片共有 {count:,} 帧，超过单个审核工单的 {MAX_REVIEW_FRAMES:,} 帧上限")


def check_review_frame_limit(count):
    if type(count) is not int or count < 1:
        raise ValueError("审核帧数无效")
    if count > MAX_REVIEW_FRAMES:
        raise ReviewFrameLimitError(count)


class OSSRedirect(Exception):
    def __init__(self, url):
        self.url = url


class OSSAccessDenied(ValueError):
    """The current OSS download URL no longer authorizes access."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def valid_digest(value):
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("文件摘要格式错误")
    return value


def opaque(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("服务端标识格式错误")
    return quote(value, safe="")


def key(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


class Client:
    def __init__(self, server, token, *, test_http=False, certificates=None):
        parsed = urlsplit(server)
        local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") not in ("", "/api/v1")
                or not (parsed.scheme == "https" or (parsed.scheme == "http" and local and test_http))):
            raise ValueError("请输入 HTTPS 服务地址")
        self.base = server.rstrip("/")
        if not self.base.endswith("/api/v1"):
            self.base += "/api/v1"
        self.token = token
        handlers = [NoRedirects()]
        if parsed.scheme == "https" and certificates is not None:
            host = quote(parsed.hostname, safe="")
            ca_file = certificates / f"{host}-{parsed.port or 443}.pem"
            if ca_file.is_file():
                context = ssl.create_default_context()
                context.load_verify_locations(cafile=str(ca_file))
                handlers.append(HTTPSHandler(context=context))
        self.opener = build_opener(*handlers)
        self.oss_opener = build_opener(NoRedirects())

    def open(self, method, path, *, body=None, headers=None, timeout=12, oss_redirect=False):
        request_headers = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=request_headers, method=method)
        try:
            return self.opener.open(request, timeout=timeout)
        except HTTPError as response:
            with response:
                if oss_redirect and response.code == 302:
                    raise OSSRedirect(oss_url(response.headers.get("Location"))) from None
                retry_after = response.headers.get("Retry-After", "")
                try:
                    error = json.loads(response.read(65536))
                except (ValueError, OSError):
                    error = {}
                if not isinstance(error, dict):
                    error = {}
                code = error.get("code", "request_rejected")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                    code = "request_rejected"
                raise ProtocolError(response.code, code, response.code in {408, 429, 500, 502, 503, 504}
                                    or error.get("retryable") is True,
                                    int(retry_after) if retry_after.isdecimal() else None) from None
        except (URLError, socket.timeout, ConnectionError, http.client.HTTPException):
            raise ProtocolError(0, "network_unavailable", True) from None

    def open_oss(self, url, position):
        headers = {"Range": f"bytes={position}-"} if position else {}
        try:
            return self.oss_opener.open(Request(oss_url(url), headers=headers, method="GET"), timeout=15)
        except HTTPError as response:
            with response:
                if response.code in {408, 429, 500, 502, 503, 504}:
                    raise ProtocolError(response.code, "oss_download_unavailable", True) from None
                error = OSSAccessDenied if response.code in {401, 403} else ValueError
                raise error(f"OSS 下载失败（HTTP {response.code}）") from None
        except (URLError, socket.timeout, ConnectionError, http.client.HTTPException):
            raise ProtocolError(0, "oss_download_unavailable", True) from None

    def json(self, method, path, *, body=None, headers=None, timeout=12):
        try:
            with self.open(method, path, body=body, headers=headers, timeout=timeout) as response:
                wait = response.headers.get("Retry-After", "")
                if response.status == 204:
                    return None, int(wait) if wait.isdecimal() else 3
                return json.load(response), None
        except (socket.timeout, ConnectionError, http.client.HTTPException):
            raise ProtocolError(0, "network_unavailable", True) from None


class Transfer:
    def __init__(self, client, assignment, stop, cache):
        self.api = client
        self.work = opaque(assignment["work_id"])
        lease = assignment["lease"]
        self.headers = {"X-Lease-Id": opaque(lease["lease_id"]), "X-Fence": str(lease["fence"]),
                        "X-Attempt": str(lease["attempt"])}
        self.stop = stop
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    def retry(self, action):
        delay = 1
        while not self.stop.is_set():
            try:
                return action()
            except ProtocolError as exc:
                if not exc.retryable or exc.status in {401, 403, 409, 422}:
                    raise
                self.stop.wait(max(delay, min(exc.retry_after or 0, 60)))
                delay = min(delay * 2, 15)
            except (socket.timeout, ConnectionError, http.client.IncompleteRead):
                self.stop.wait(delay)
                delay = min(delay * 2, 15)
        raise Interrupted

    def json(self, method, path, body=None, action=None, extra=None):
        headers = {**self.headers, **(extra or {})}
        if action:
            headers["Idempotency-Key"] = key(self.work, self.headers["X-Lease-Id"], action)
        return self.retry(lambda: self.api.json(method, path, body=body, headers=headers)[0])

    def download(self, artifact, working, *, source_url=None):
        from .state import slot

        expected = valid_digest(artifact["sha256"])
        size = artifact["size_bytes"]
        target = self.cache / expected
        partial = working / (expected + ".part")
        path = f"/artifacts/{opaque(artifact['artifact_id'])}/content?work_id={self.work}"
        etag = f'"{expected}"'

        def transfer():
            nonlocal source_url
            if target.is_file() and target.stat().st_size == size and digest(target) == expected:
                return target
            position = partial.stat().st_size if partial.exists() else 0
            if position == size and digest(partial) == expected:
                partial.replace(target)
                return target
            if position >= size:
                partial.unlink(missing_ok=True)
                position = 0
            headers = dict(self.headers)
            if position:
                headers.update(Range=f"bytes={position}-", **{"If-Range": etag})
            external = False
            if source_url is not None:
                try:
                    response = self.api.open_oss(source_url, position)
                    external = True
                except OSSAccessDenied:
                    # A claim URL can expire before production resumes. Refresh through the manager.
                    source_url = None
            if not external:
                try:
                    response = self.api.open("GET", path, headers=headers, oss_redirect=True)
                except OSSRedirect as redirect:
                    external = True
                    response = self.api.open_oss(redirect.url, position)
            with response:
                if not external and response.headers.get("ETag") != etag:
                    raise ValueError("下载对象 ETag 与冻结清单不符")
                if response.status == 206:
                    if response.headers.get("Content-Range") != f"bytes {position}-{size-1}/{size}":
                        raise ValueError("续传字节范围错误")
                elif response.status == 200:
                    position = 0
                else:
                    raise ValueError("下载响应状态错误")
                with partial.open("ab" if position else "wb") as output:
                    while block := response.read(1024 * 1024):
                        if self.stop.is_set():
                            raise Interrupted
                        position += len(block)
                        if position > size:
                            raise ValueError("下载内容超过已登记长度")
                        output.write(block)
            if position != size:
                raise ProtocolError(0, "incomplete_download", True)
            if digest(partial) != expected:
                partial.unlink()
                raise ValueError("下载对象 SHA-256 校验失败")
            partial.replace(target)
            return target

        # Two leases may need the same source or rule bundle. Serialize publication of
        # each digest so Windows never has to replace the object a sibling is reading.
        with slot(self.cache.parent / "locks", expected[:2], 1, self.stop):
            return self.retry(transfer)

    def upload(self, role, path: Path, media_type, progress=lambda *_: None, *, expected_sha=None):
        size = path.stat().st_size
        sha = digest(path)
        if expected_sha is not None and sha != expected_sha:
            raise ValueError("产物在上传前发生变化")
        body = dict(role=role, sha256=sha, size_bytes=size, media_type=media_type)
        started = self.json("POST", f"/work/{self.work}/uploads", body, f"begin:{role}:{sha}")
        upload = f"/uploads/{opaque(started['upload_id'])}"
        current = self.json("GET", upload)
        part_size = current["part_size_bytes"]
        count = (size + part_size - 1) // part_size
        if (current["sha256"], current["size_bytes"], current["role"], current["part_count"], part_size) != (
                sha, size, role, count, 8388608):
            raise ValueError("上传会话与本地产物不符")
        if current["state"] == "complete":
            return self._check_artifact(current["artifact"], body)
        parts = {part["part_no"]: part for part in current["received_parts"]}
        with path.open("rb") as file:
            for i in range(1, count + 1):
                start = (i - 1) * part_size
                content = file.read(min(part_size, size - start))
                end = start + len(content) - 1
                part_hash = hashlib.sha256(content).hexdigest()
                if parts.get(i) != dict(part_no=i, start=start, end=end, sha256=part_hash):
                    self.json("PUT", f"{upload}/parts/{i}", content, extra={
                        "Content-Type": "application/octet-stream", "X-Part-SHA256": part_hash,
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    })
                progress(end + 1, size)
        sealed = self.json("POST", upload + "/complete", dict(sha256=sha, part_count=count), f"seal:{role}:{sha}")
        return self._check_artifact(sealed, body)

    @staticmethod
    def _check_artifact(artifact, body):
        if any(artifact.get(k) != v for k, v in body.items()):
            raise ValueError("服务端返回的产物摘要或类型不符")
        opaque(artifact["artifact_id"])
        return artifact
