"""Render and encode a full-timeline comparison for the first authoring pass."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from film_worker.actor_guard import ACTOR_GEOMETRY
from film_worker.media import (add_audio, comparison, encode_images, even, frame_path, intake,
                               read_json, rgb, validate_encoded, write_json)
from film_worker.progress import Progress

BASE = Path(__file__).resolve().parent


MOVIE = "首版自检对比.mp4"
STAMP = ".draft-review.json"
REVIEW_VERSION = 2


def current(stage):
    movie, stamp, scene = (stage / name for name in (MOVIE, STAMP, "scene.blend"))
    if not (movie.is_file() and stamp.is_file() and scene.is_file()):
        return False
    try:
        recorded = read_json(stamp)
    except ValueError:
        return False
    return recorded == {"version": REVIEW_VERSION, "scene": intake.digest(scene), "movie": intake.digest(movie)}


def generate(stage, source, blender, threads, backend="cpu", *, progress=None):
    if current(stage):
        return stage / MOVIE
    scene = stage / "scene.blend"
    scene_hash = intake.digest(scene)
    timeline_path = Path(read_json(stage / "draft-context.json")["timeline"])
    timeline = read_json(timeline_path)
    if intake.digest(source) != timeline["source_sha256"]:
        raise ValueError("自检片原片与作业时间线不一致")
    size = (timeline["width"], timeline["height"])
    scale = min(Fraction(1), Fraction(480, max(size)))
    width, height = (even(dimension * scale) for dimension in size)
    pair_size = width, 2 * height

    with tempfile.TemporaryDirectory(dir=stage, prefix=".draft-review-") as temp:
        folder = Path(temp)
        frames = folder / "frames"
        candidate = folder / "scene.blend"
        shutil.copyfile(scene, candidate)
        command = [sys.executable, str(BASE / "blender_author.py"), blender, "-b",
                   "--disable-autoexec", str(candidate), "--python-exit-code", "1", "-P"]
        subprocess.run([*command, str(BASE / "prepare_render.py")],
                       env=dict(os.environ, MOVIE_JOB_TIMELINE=str(timeline_path), MOVIE_BUILTIN_RULES="1",
                                MOVIE_ACTOR_GEOMETRY=str(stage / ACTOR_GEOMETRY)), check=True)
        render = [*command, str(BASE / "render_frames.py"), "--", str(frames), backend,
                  str(width), str(height), str(threads), "--draft",
                  "--cache-dir", str(stage.parent / ".gpu-cache")]
        if progress is None:
            subprocess.run(render, check=True)
        else:
            count = len(timeline["pts"])
            render_stage = "render_draft_" + backend
            progress(render_stage, 0, count, render_size=(width, height))
            with subprocess.Popen(render) as child:
                while True:
                    try:
                        child.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        progress(render_stage, sum(1 for path in frames.glob("*.png") if path.stem.isdecimal()), count)
                if child.returncode:
                    raise subprocess.CalledProcessError(child.returncode, render)
            progress("encode_draft")
        silent, encoded = folder / "silent.mp4", folder / MOVIE

        def pair(number):
            return comparison(timeline_path.parent / "frames", rgb(frame_path(frames, number)), number)

        encode_images(silent, timeline, pair_size, pair, preview=True)
        add_audio(silent, encoded, timeline, source)
        if progress is not None:
            progress("verify_draft")
        validate_encoded(encoded, timeline, pair_size, preview=True)
        if intake.digest(scene) != scene_hash:
            raise ValueError("生成自检片时工程发生变化，请再次生成")
        write_json(folder / STAMP, {"version": REVIEW_VERSION, "scene": scene_hash, "movie": intake.digest(encoded)})
        encoded.replace(stage / MOVIE)
        (folder / STAMP).replace(stage / STAMP)
    return stage / MOVIE


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("blender")
    parser.add_argument("threads", type=int)
    parser.add_argument("--backend", default="cpu", choices=("cpu", "metal", "cuda", "optix", "hip", "oneapi"))
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--report-progress", action="store_true")
    args = parser.parse_args()
    for name in ("ffmpeg", "ffprobe"):
        if executable := getattr(args, name):
            os.environ["FILM_WORKER_" + name.upper()] = executable
    stage = args.stage.resolve()
    progress = Progress(stage.parent) if args.report_progress else None
    print(generate(stage, args.source.resolve(), args.blender, args.threads, args.backend, progress=progress))
