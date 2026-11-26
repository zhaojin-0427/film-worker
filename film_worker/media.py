"""Frame-accurate source intake, comparison pictures and final encoding."""
import hashlib
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import av
from av.video.reformatter import VideoReformatter
from PIL import Image


from . import intake
from .artifact_staging import staging_directory
from .protocol import check_review_frame_limit


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def ffmpeg(*args):
    subprocess.run([os.environ.get("FILM_WORKER_FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-n", *map(str, args)],
                   check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def even(value):
    return max(2, 2 * round(value / 2))


def review_size(timeline):
    width, height = timeline["width"], timeline["height"]
    max_width, max_height = (1920, 1080) if width >= height else (1080, 1920)
    scale = min(Fraction(1), Fraction(max_width, width), Fraction(max_height, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def prepare_source(directory, source, requested_width):
    ready = directory / "intake"
    if ready.is_dir():
        timeline = read_json(ready / "timeline.json")
        check_review_frame_limit(len(timeline["pts"]))
        return timeline
    metadata, video, pts, base, duration, _ = intake.measure(source)
    check_review_frame_limit(len(pts))
    sar = video.get("sample_aspect_ratio", "1:1")
    sar = Fraction(sar.replace(":", "/")) if sar not in ("0:1", "N/A", None) else Fraction(1)
    dw, dh = Fraction(video["width"]) * sar, Fraction(video["height"])
    angle = next((x["rotation"] for x in video.get("side_data_list", []) if "rotation" in x), 0)
    if round(angle) % 180 == 90:
        dw, dh = dh, dw
    width = even(requested_width or dw)
    height = even(width * dh / dw)
    audio = next((s for s in metadata["streams"] if s["codec_type"] == "audio"), None)
    timeline = dict(pts=[t - pts[0] for t in pts], time_base=str(base), end=int(duration / base),
                    duration=str(duration), fps=str(len(pts) / duration), source_sha256=intake.digest(source),
                    width=width, height=height, display_size=[round(dw), round(dh)], source_size=[video["width"], video["height"]],
                    audio_index=audio["index"] if audio else None, audio_offset="0")
    boundaries = timeline["pts"] + [timeline["end"]]
    timeline["variable_rate"] = len({b-a for a,b in zip(boundaries, boundaries[1:])}) > 1
    if audio:
        origin = (Fraction(audio["time_base"]) * int(audio["start_pts"]) if "start_pts" in audio
                  else Fraction(audio.get("start_time", "0")))
        timeline["audio_offset"] = str(origin - pts[0] * base)
    with staging_directory(directory, prefix="intake-") as tmp:
        stage = tmp / "intake"
        frames = stage / "frames"
        frames.mkdir(parents=True)
        transfer = video.get("color_transfer")
        hdr_options = ""
        if transfer in ("smpte2084", "arib-std-b67"):
            hdr_options = (f":in_transfer={transfer}:out_primaries=bt709:out_transfer=bt709"
                           ":out_color_matrix=bt709:intent=perceptual")
        filters = [f"scale={even(dw)}:{even(dh)}:flags=lanczos{hdr_options}", "setsar=1", "format=rgb24"]
        ffmpeg("-i", source, "-map", f"0:{video['index']}", "-an", "-vf", ",".join(filters),
               "-fps_mode", "passthrough", "-start_number", "1", frames / "%04d.png")
        if len(list(frames.glob("[0-9]*.png"))) != len(pts):
            raise ValueError("源片解码后的图片数量与显示时间戳数量不同")
        intake.overview(source, stage / "overview.jpg", video["index"], len(pts), scale_options=hdr_options)
        write_json(stage / "probe.json", metadata)
        write_json(stage / "timeline.json", timeline)
        os.rename(stage, ready)
    return timeline


def frame_path(directory, number):
    return directory / f"{number:04d}.png"


def rgb(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def comparison(source_dir, white, number):
    upper = rgb(frame_path(source_dir, number))
    if upper.size != white.size:
        upper = upper.resize(white.size, Image.Resampling.LANCZOS)
    pair = Image.new("RGB", (white.width, white.height * 2))
    pair.paste(upper, (0, 0))
    pair.paste(white, (0, white.height))
    return pair


def signature(pair, timeline, number):
    pts = timeline["pts"]
    sig = hashlib.sha256()
    sig.update(json.dumps([timeline["source_sha256"], number, pts[number-1], timeline["time_base"],
                           pts[number] if number < len(pts) else timeline["end"], pair.size]).encode())
    sig.update(pair.tobytes())
    return sig.hexdigest()


def compare_version(stage, source_dir, timeline, progress, previous=None, size=None):
    pairs = stage / "pairs"
    pairs.mkdir(exist_ok=True)
    count = len(timeline["pts"])
    old = []
    old_image_hashes = None
    if previous:
        try:
            manifest = read_json(previous / "manifest.json")
            if isinstance(manifest, dict) and isinstance(manifest.get("signatures"), list):
                old = manifest["signatures"]
                old_image_hashes = manifest.get("review_image_sha256")
        except (OSError, ValueError):
            pass
    signatures = []
    for number in range(1, count + 1):
        white = rgb(frame_path(stage / "white", number))
        if white.size != (size or (timeline["width"], timeline["height"])):
            raise ValueError(f"第 {number} 帧的白模尺寸与送审画幅不符")
        pair = comparison(source_dir, white, number)
        current = signature(pair, timeline, number)
        signatures.append(current)
        filename = f"{number:04d}.jpg"
        prior_image = previous / "pairs" / filename if previous else None
        temporary = pairs / f"{number:04d}.writing.jpg"
        reuse = len(old) == count and old[number - 1] == current and prior_image.is_file()
        if reuse and old_image_hashes is not None:
            reuse = (isinstance(old_image_hashes, list) and len(old_image_hashes) == count
                     and intake.digest(prior_image) == old_image_hashes[number - 1])
        if reuse:
            shutil.copyfile(prior_image, temporary)
        else:
            pair.save(temporary, quality=94, subsampling=0)
        temporary.replace(pairs / filename)
        if number == count or number % 20 == 0:
            progress(f"比对并整理图片 {number}/{count}")
    return signatures


def encode_images(path, timeline, size, get_image, *, preview=False):
    base = Fraction(timeline["time_base"])
    pts = timeline["pts"]
    durations = dict(zip(pts, [b-a for a, b in zip(pts, pts[1:] + [timeline["end"]])]))
    options = {"video_track_timescale": str(base.denominator)}
    if preview:
        options["movflags"] = "+faststart"
    with av.open(str(path), "w", options=options) as output:
        stream = output.add_stream("libx264", rate=Fraction(timeline["fps"]).limit_denominator(65535))
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        stream.time_base = stream.codec_context.time_base = base
        stream.codec_context.max_b_frames = 0
        stream.codec_context.thread_count = 3
        stream.codec_context.colorspace = stream.codec_context.color_primaries = stream.codec_context.color_trc = 1
        stream.codec_context.color_range = 1
        settings = {"preset": "veryfast", "tune": "zerolatency"}
        if preview:
            settings["crf"] = "18"
        else:
            stream.bit_rate = 30_000_000
            settings["x264-params"] = "nal-hrd=cbr:vbv-maxrate=30000:vbv-bufsize=30000:vbv-init=1:filler=1"
        stream.options = settings
        convert = VideoReformatter()

        def mux(packets):
            for packet in packets:
                tick = Fraction(packet.pts) * packet.time_base / base
                packet.duration = int(durations[tick] * base / packet.time_base)
                output.mux(packet)

        for number, tick in enumerate(pts, 1):
            image = get_image(number)
            if image.size != size:
                raise ValueError(f"第 {number} 帧的原始尺寸 {image.size} 与编码画幅 {size} 不符")
            frame = convert.reformat(av.VideoFrame.from_image(image), format="yuv420p",
                                     dst_colorspace="ITU709", src_color_range="JPEG", dst_color_range="MPEG")
            frame.time_base, frame.pts = base, tick
            mux(stream.encode(frame))
        mux(stream.encode(None))


def add_audio(silent, destination, timeline, source):
    if timeline["audio_index"] is None:
        silent.rename(destination)
        return
    offset = round(Fraction(timeline["audio_offset"]) * 48000)
    shift = f"adelay={offset}S:all=1" if offset >= 0 else f"atrim=start_sample={-offset}"
    samples = round(Fraction(timeline["duration"]) * 48000)
    ffmpeg("-i", silent, "-i", source, "-map", "0:v:0", "-c:v", "copy", "-map", f"1:{timeline['audio_index']}",
           "-af", f"aresample=48000,{shift},asetpts=PTS-STARTPTS,apad,atrim=end_sample={samples}",
           "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "320k", "-map_metadata", "-1",
           "-video_track_timescale", Fraction(timeline["time_base"]).denominator, "-movflags", "+faststart", destination)
    silent.unlink()


def validate_encoded(path, timeline, size, *, preview=False):
    metadata, video, pts, base, duration, _ = intake.measure(path)
    source_base = Fraction(timeline["time_base"])
    if ((video["width"], video["height"]) != size or video["codec_name"] != "h264" or
        len(pts) != len(timeline["pts"]) or duration != Fraction(timeline["duration"]) or
        any(t * base != ref * source_base for t, ref in zip(pts, timeline["pts"]))):
        raise ValueError(f"编码后帧数、时间戳、时长或画幅核验失败：{path.name}")
    if not preview and int(video.get("bit_rate", 0)) < 18_000_000:
        raise ValueError(f"编码后视频码率低于 18 Mbps：{path.name}")
    tracks = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    if timeline["audio_index"] is None:
        valid_audio = not tracks
    else:
        valid_audio = len(tracks) == 1 and tracks[0]["codec_name"] == "aac" and tracks[0]["channels"] == 2 and int(
            tracks[0]["sample_rate"]) == 48000 and Fraction(tracks[0].get("start_time", "0")) == 0
    if not valid_audio:
        raise ValueError(f"编码后音轨核验失败：{path.name}")


def deliver(directory, source, job_name, version, timeline, destination, progress, white_frames=None):
    name = Path(job_name).stem or "成片"
    source_frames = directory / "intake/frames"
    white_frames = version / "white" if white_frames is None else white_frames
    width, height = timeline["width"], timeline["height"]
    for folder in ("target", "comparison", "white_model", "model_3d", "meta"):
        (destination / folder).mkdir(parents=True)
    shutil.copy2(version / "scene.blend", destination / "model_3d" / (name + ".blend"))
    with (destination / "meta" / (name + ".jsonl")).open("w", encoding="utf-8") as output:
        for line in (version / "meta.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                entry.update(sample_id=name, production_method="Codex 内部试制")
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")
    exports = [
        ("white_model", name + "白模.mp4", (width, height), lambda n: rgb(frame_path(white_frames, n))),
        ("comparison", name + "对比.mp4", (width, height*2),
         lambda n: comparison(source_frames, rgb(frame_path(white_frames, n)), n)),
    ]
    if source.suffix.lower() == ".mp4":
        shutil.copy2(source, destination / "target" / (name + ".mp4"))
    else:
        with Image.open(frame_path(source_frames, 1)) as first:
            native = first.size
        exports.append(("target", name + ".mp4", native, lambda n: rgb(frame_path(source_frames, n))))
    for folder, filename, size, get_image in exports:
        progress(f"正在编码 {filename}")
        silent = destination / "video-only.mp4"
        out = destination / folder / filename
        encode_images(silent, timeline, size, get_image)
        add_audio(silent, out, timeline, source)
        validate_encoded(out, timeline, size)
