"""Node token in the macOS Keychain or Windows Credential Manager."""
import hashlib
import json
import os
import subprocess


def label(root):
    return "film-worker/" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]


def read_token(root):
    inherited = os.environ.get("FILM_WORKER_TOKEN")
    if inherited:
        return inherited
    return read_secret(label(root))


def read_secret(target):
    if os.name == "nt":
        import win32cred
        from pywintypes import error
        try:
            saved = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        except error as exc:
            if exc.winerror == 1168:
                return ""
            raise windows_credential_error(exc, "读取") from exc
        return saved["CredentialBlob"].decode("utf-16-le")
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", target, "-w"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def store_token(root, token):
    if not token.isascii() or not 1 <= len(token) <= 1024 or any(char.isspace() for char in token):
        raise ValueError("建模 Token 格式错误")
    store_secret(label(root), token)


def read_oss_keys(root):
    value = read_secret(label(root) + "/oss")
    if not value:
        return None
    keys = json.loads(value)
    if not isinstance(keys, list) or len(keys) != 2 or not all(isinstance(key, str) and key for key in keys):
        raise ValueError("本机 OSS AccessKey 记录无效，请重新保存")
    return tuple(keys)


def store_oss_keys(root, access_key_id, access_key_secret):
    if not all(isinstance(value, str) and 1 <= len(value) <= 128 and value.isascii()
               and all(33 <= ord(char) <= 126 for char in value)
               for value in (access_key_id, access_key_secret)):
        raise ValueError("请同时填写有效的 OSS AccessKey ID 和 Secret")
    store_secret(label(root) + "/oss", json.dumps([access_key_id, access_key_secret]))


def store_secret(target, value):
    if os.name == "nt":
        import win32cred
        from pywintypes import error
        try:
            win32cred.CredWrite(dict(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target,
                                    Persist=win32cred.CRED_PERSIST_LOCAL_MACHINE,
                                    CredentialBlob=value), 0)
        except error as exc:
            raise windows_credential_error(exc, "保存") from exc
    else:
        # Feed security's command interpreter via stdin so the secret stays out of process arguments.
        import shlex
        line = 'add-generic-password -U -s ' + target + ' -a worker -w ' + shlex.quote(value)
        result = subprocess.run(["/usr/bin/security", "-i"], input=line+"\n", capture_output=True,
                                text=True, check=False)
        if result.returncode != 0 or read_secret(target) != value:
            raise ValueError("macOS 钥匙串保存失败")


def windows_credential_error(exc, operation):
    if exc.winerror == 1312:
        return ValueError("当前 Windows 登录会话无法访问凭据管理器。请在桌面会话配置建模 Token，"
                          "或在部署环境设置 FILM_WORKER_TOKEN")
    return ValueError(f"Windows 凭据管理器{operation}失败（错误 {exc.winerror}）")
