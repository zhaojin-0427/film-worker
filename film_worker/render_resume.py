"""Recover validated rendering inputs and PNGs from earlier local attempts."""
import shutil

from .artifact_staging import staging_directory
from .media import frame_path, read_json
from .production_helpers import verify_rendered_frame
from .protocol import digest
from .settings import atomic_json


def local_path(folder, *parts):
    path = folder.joinpath(*parts)
    if path.resolve() != path:
        raise ValueError("渲染断点路径不能指向其他目录")
    return path


def previous_attempts(directory, request):
    assignment = request["assignment"]
    lease = assignment.get("lease", {})
    attempt, device = lease.get("attempt"), lease.get("worker_id")
    if (type(attempt) is not int or attempt < 2 or not device or directory.name != str(attempt)
            or directory.parent.name != assignment.get("work_id") or directory.parent.parent.name != "work"):
        return []

    candidates = []
    parent = directory.parent.resolve()
    for folder in parent.iterdir():
        if folder.name.isascii() and folder.name.isdecimal():
            number = int(folder.name)
            if 0 < number < attempt and folder.name == str(number):
                candidates.append((number, folder))

    expected = (assignment["work_id"], assignment["job_id"], assignment["input_manifest"]["kind"],
                assignment["input_manifest_sha256"], (assignment.get("oss") or {}).get("protocol"))
    compatible = []
    for number, folder in sorted(candidates, reverse=True):
        try:
            old = read_json(local_path(parent, folder.name, "request.json"))
            prior = old["assignment"]
            identity = (prior["work_id"], prior["job_id"], prior["input_manifest"]["kind"],
                        prior["input_manifest_sha256"], (prior.get("oss") or {}).get("protocol"))
            prior_env, current_env = old["environment"], request["environment"]
            prior_profile = old.get("production") or prior["input_manifest"].get("profile") or {}
            profile = request.get("production") or assignment["input_manifest"].get("profile") or {}
            if (identity != expected or prior["input_manifest"] != assignment["input_manifest"]
                    or prior["lease"]["attempt"] != number or prior["lease"]["worker_id"] != device
                    or old["tools"]["blender"] != request["tools"]["blender"]
                    or old["cpu_threads"] != request["cpu_threads"]
                    or prior_env["render_backend"] != current_env["render_backend"]
                    or any(prior_env["compatibility"].get(k) != current_env["compatibility"].get(k)
                           for k in ("blender", "decoder_id"))
                    or any(prior_profile.get(k) != profile.get(k) for k in
                           ("final_width", "review_landscape_limit", "review_portrait_limit", "production_policy"))):
                continue
            compatible.append(folder)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return compatible


def matching_frames(attempt, name, tag):
    try:
        folder = local_path(attempt, name)
        if read_json(local_path(folder, "checkpoint.json")) == tag:
            return folder
    except (OSError, ValueError):
        pass
    return None


def reuse_frames(attempts, target, tag):
    reused = 0
    for attempt in attempts:
        if not (old := matching_frames(attempt, target.name, tag)):
            continue
        with staging_directory(target, prefix=".resume-") as staging:
            for number in range(1, tag["count"] + 1):
                path = frame_path(target, number)
                if path.is_file():
                    continue
                temporary = staging / path.name
                try:
                    source = local_path(old, path.name)
                    if not source.is_file():
                        continue
                    shutil.copyfile(source, temporary)
                    verify_rendered_frame(temporary, tuple(tag["dimensions"]))
                except (OSError, ValueError, SyntaxError):
                    continue
                temporary.replace(path)
                reused += 1
    return reused


def restore_review(attempts, root, tag, expected, *, auxiliary, compatibility):
    scene, meta = "scene.blend", "meta.jsonl"
    name = "review-" + tag["backend"]
    for attempt in attempts:
        try:
            saved = read_json(local_path(attempt, "prepared.json"))
            if saved["input"] != expected or saved["environment"]["render_backend"] != tag["backend"]:
                continue
            repair_file = local_path(attempt, "visual-repair.json")
            repair = read_json(repair_file) if repair_file.is_file() else {}
            if repair.get("input") == expected and "issues" in repair:
                return False
            render_tag = dict(tag, scene_sha256=saved["scene"])
            if not (frames := matching_frames(attempt, name, render_tag)):
                return False
            if not any(path.is_file() and path.stem.isascii() and path.stem.isdecimal()
                       for path in frames.glob("*.png")):
                return False
            old_stage = local_path(attempt, "scene")
            with staging_directory(root, prefix=".resume-scene-") as staging:
                for filename in (scene, meta, *auxiliary):
                    source = local_path(old_stage, filename)
                    if filename in (scene, meta) or source.is_file():
                        shutil.copyfile(source, staging / filename)
                if digest(staging / scene) != saved["scene"] or digest(staging / meta) != saved["meta"]:
                    return False
                # The prepared marker is published only after all inputs have been copied.
                for path in staging.iterdir():
                    path.replace(root / "scene" / path.name)
                if repair.get("input") == expected:
                    atomic_json(root / "visual-repair.json", repair)
                atomic_json(root / "prepared.json", dict(
                    saved, environment=dict(saved["environment"], compatibility=compatibility)))
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            if (root / "scene" / scene).exists() or (root / "scene" / meta).exists():
                raise
    return False
