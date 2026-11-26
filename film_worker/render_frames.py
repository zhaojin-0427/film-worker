"""Render one specified size from the immutable prepared Blender scene."""
import argparse
import os
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engineering_checks import require_cycles


def render(output, backend, width, height, threads, *, draft=False, cache_dir=None, numbers=None):
    scene = bpy.context.scene
    if draft:
        require_cycles(scene.render.engine)
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    if (scene.render.resolution_x, scene.render.resolution_y) != (width, height):
        raise ValueError(f"Blender 不支持该渲染尺寸 {width}×{height}")
    scene.render.threads_mode = "FIXED"
    scene.render.threads = threads
    if scene.render.engine == "CYCLES":
        if draft:
            scene.cycles.samples = 2
        if backend != "cpu":
            if backend in {"optix", "cuda"}:
                cache = (cache_dir or output.parent / ".gpu-cache") / backend
                cache.mkdir(parents=True, exist_ok=True)
                os.environ["OPTIX_CACHE_PATH" if backend == "optix" else "CUDA_CACHE_PATH"] = str(cache)
            preferences = bpy.context.preferences.addons["cycles"].preferences
            device_type = backend.upper()
            preferences.compute_device_type = device_type
            devices = preferences.get_devices_for_type(device_type)
            for device in devices:
                device.use = device.type == device_type
            if not any(device.use for device in devices):
                raise RuntimeError(f"无可用的 {device_type} 渲染设备")
            scene.cycles.device = "GPU"
            scene.cycles.denoising_use_gpu = True
            if scene.render.resolution_x * scene.render.resolution_y <= 3840 * 2160:
                scene.cycles.use_auto_tile = False
        else:
            scene.cycles.device = "CPU"

    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "rendering.png"
    if numbers and any(not scene.frame_start <= n <= scene.frame_end for n in numbers):
        raise ValueError("试渲帧号超出工程时间线")
    for number in numbers or range(scene.frame_start, scene.frame_end + 1):
        target = output / f"{number:04d}.png"
        if target.exists():
            continue
        temporary.unlink(missing_ok=True)
        scene.frame_set(number)
        scene.render.filepath = str(temporary)
        if "FINISHED" not in bpy.ops.render.render(write_still=True):
            raise RuntimeError(f"第 {number} 帧渲染未完成")
        temporary.replace(target)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
parser.add_argument("backend", choices=("metal", "cpu", "cuda", "optix", "hip", "oneapi"))
parser.add_argument("width", type=int)
parser.add_argument("height", type=int)
parser.add_argument("threads", type=int)
parser.add_argument("--draft", action="store_true")
parser.add_argument("--cache-dir", type=Path)
parser.add_argument("--frames", nargs="+", type=int)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
render(args.output.resolve(), args.backend, args.width, args.height, args.threads, draft=args.draft,
       cache_dir=args.cache_dir, numbers=args.frames)
