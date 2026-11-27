"""Production guidance and verifiable media acceptance checks."""
import json
from fractions import Fraction

import av
from PIL import Image

from . import intake


L3_RULES = """制作与审核共用的 L3 白模规则
使用甲方指定的 L3。保留原始网格、比例、表面结构、材质特性及面部、胸口朝向标记。角色色只按工作台固定配色，同一角色跨镜一致，原片服装颜色只用于识别角色。
禁止更换、重塑或拉伸人体网格。即使原网格未改，也禁止单独建模或挂接衣服、帽子、兜帽、头发、五官、额外手掌或手指，禁止通过贴图或材质模拟这些外观。发现违规部件或材质改造时，移除并恢复基础白模。
人物还原只调整位置、朝向、关节姿态和动作关键帧。构图、占画、裁切和透视通过镜头调整。分开核对骨盆、胸口、头和四肢，检查屈膝、足距、脚底接触、重心转移和支撑。根据遮挡或出画前后的连续帧做最小推断，保留可见的细微动作、停顿和节奏。
"""


REVIEW_CHECKLIST = """逐镜回看。按源帧核对以下十类缺陷，返修时同时检查本段、相邻帧和全片同类问题。
1. 比例与尺度。按门窗、道具和其他人物核对头身比、四肢长度和物体体量。
2. 透视与视角。核对消失点、地平线、俯仰、焦距、运镜种类和镜头加减速。
3. 空间对位。对齐主角和关键物体的投影轮廓、中心、落点、边界和前后层次。
4. 模型结构。应影响透视、接触或遮挡的门窗、家具和结构必须补齐；部件完整且连接正确。
5. 穿模。检查角色与地板、墙、道具和其他角色的非预期相交。
6. 姿态与动作。核对朝向、肩颈、重心、步态、脚掌落地、白模现有手部与道具的实际接触。
7. 动画时序。检查手部和关节抽搐、卡顿、步速错位及片尾动作提前停止。
8. 渲染闪烁与抖动。检查静态背景、门框、灯光、边缘及亮度跳变。
9. 时序连续性。切镜、遮挡和出入画时机要一致；核对物体和人是否突然出现或消失。
10. 色彩区分。同一角色跨镜保持工作台固定角色色，多角色、道具与场景可区分。
重点查看每次切镜、接触、遮挡、出入画以及末尾一秒，检查黑帧、空镜、无关音画。
若原片难以识别、误差过大或暂时不能修复，在制作记录标出帧段及原因，由人工核对。
"""


def policy(profile):
    return profile.get("production_policy", "codex_trial")


def may_author(profile, kind):
    return policy(profile) != "human_required" or kind == "finalize"


def detect_black_frames(path, video_stream, frame_count):
    black = []
    decoded = 0
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams.video if s.index == video_stream)
        for decoded, frame in enumerate(container.decode(stream),1):
            gray = frame.reformat(format="gray")
            plane = gray.planes[0]
            image = Image.frombytes("L", (gray.width, gray.height), bytes(plane), "raw", "L", plane.line_size, 1)
            if image.getextrema()[1] <= 5:
                black.append(decoded)
    if decoded != frame_count:
        raise ValueError("黑帧检查的解码帧数与时间线不一致")
    return black


def read_video(role, path, *, scan_black=False):
    raw, video, pts, time_base, duration, _ = intake.measure(path)
    tracks = [stream for stream in raw["streams"] if stream["codec_type"] == "audio"]
    black_frames = detect_black_frames(path, video["index"], len(pts)) if scan_black else []
    starts = [tick-pts[0] for tick in pts]
    start_of_last_second = max(Fraction(0), duration-1)
    tail = [i for i in black_frames if (starts[i] if i < len(starts) else duration/time_base)*time_base >
            start_of_last_second]
    fmt = raw["format"]["format_name"]
    return dict(role=role, container=fmt, codec=video["codec_name"],
                width=video["width"], height=video["height"], duration_seconds=str(duration),
                frame_count=len(pts), average_fps=str(len(pts)/duration), time_base=str(time_base),
                variable_rate=len(set(b-a for a,b in zip(starts, starts[1:]+[int(duration/time_base)]))) > 1,
                bitrate=int(video.get("bit_rate") or 0), black_frames=black_frames, black_tail_frames=tail,
                audio=[dict(codec=s["codec_name"], channels=s.get("channels"), sample_rate=int(s.get("sample_rate") or 0),
                            bitrate=int(s.get("bit_rate") or 0), sample_format=s.get("sample_fmt"))
                       for s in tracks])


def validate_formal(record):
    if (record["width"] < record["height"] or min(record["width"],record["height"]) < 1080 or
            Fraction(record["average_fps"]) < 24 or not 15 <= Fraction(record["duration_seconds"]) <= 40 or
            "mp4" not in record["container"].split(",") or record["codec"] not in {"h264","hevc"}):
        raise ValueError(f"{record['role']} 未达到正式批次的横幅、分辨率、时长、帧率或编码要求")


def media_report(path, sample_id, files, *, formal):
    """Save three technical JSONL rows and fail if generated outputs contain black frames."""
    rows = []
    for role, file in files:
        data = read_video(role, file, scan_black=role != "source_video" or formal)
        data.update(sample_id=sample_id, file=file.relative_to(path.parent.parent).as_posix())
        if role != "source_video" and data["audio"]:
            data["audio_target_bitrate"] = 320_000
        if formal:
            if role != "comparison_video":
                validate_formal(data)
            if role == "source_video" and file.suffix.lower() != ".mp4":
                raise ValueError("正式批次原片须为 MP4")
        rows.append(data)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False)+"\n")
    for row in rows:
        if row["black_frames"]:
            suffix = f"，片尾最后一秒 {row['black_tail_frames'][:10]}" if row["black_tail_frames"] else ""
            raise ValueError(f"{row['role']} 存在黑帧 {row['black_frames'][:10]}{suffix}")
