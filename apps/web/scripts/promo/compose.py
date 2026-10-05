"""宣传片 v2 合成：按时间线逐帧渲染 60fps 成片。

- 镜头：camera 事件 → 中心点线性缓动、缩放按对数缓动（推拉速度在感知上均匀）；
- 画面：2x 源帧按亚像素区域重采样，推近时依旧清晰，平移不抖；
- 光标：按录制时同一条缓动曲线重算，点击处画涟漪；
- 字幕 / 加速角标 / 片头片尾：屏幕坐标叠加，带淡入与位移动画；
- 加速段：speed 事件之间按倍率压缩时间，期间显示「×N 加速」。
"""

from __future__ import annotations

import bisect
import json
import math
import multiprocessing as mp
import os
import pathlib
import subprocess
import sys

import numpy as np
from PIL import Image

DIR = pathlib.Path(os.environ.get("PROMO_DIR", "/tmp/agentmem-promo"))
ASSETS = DIR / "assets"
OUT = DIR / "out"
FPS = 60
W, H = 1920, 1080
TITLE = 3.6
OUTRO = 4.6
WORKERS = int(os.environ.get("WORKERS", str(max(2, (os.cpu_count() or 4) - 1))))

layout = json.loads((ASSETS / "layout.json").read_text())
WORLD_W, WORLD_H = layout["w"], layout["h"]
WIN = layout["win"]
FULL = (WORLD_W / 2, WORLD_H / 2, WORLD_W)
MIN_VIEW = 960  # 源帧是 2x：屏幕每像素至少对应半个 CSS 像素，再推近就糊了

timeline = json.loads((DIR / "timeline.json").read_text())
FRAMES = timeline["frames"]
FRAME_T = [frame["t"] for frame in FRAMES]
EVENTS = sorted(timeline["events"], key=lambda e: e["t"])
T0, T1 = timeline["start"], timeline["end"]


def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 4 * u**3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def ease_out(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 1 - (1 - u) ** 3


# ---------------- 时间映射（加速段） ----------------
speed_marks = [(T0, 1.0, False)] + [
    (e["t"], float(e["factor"]), bool(e.get("quiet"))) for e in EVENTS if e["type"] == "speed"
]
#: 静止画面最多保留这么久：录制里为了等页面稳定、给观众读字留的停顿，超出部分自动压缩
IDLE_KEEP = 1.3


def activity_intervals() -> list[tuple[float, float]]:
    """「画面上有事发生」的区间：新帧、鼠标移动、点击、镜头运动、字幕出现后的阅读时间。"""
    spans = [(f["t"] - 0.02, f["t"] + 0.06) for f in FRAMES]
    for e in EVENTS:
        if e["type"] == "move":
            spans.append((e["t"], e["t"] + e["dur"]))
        elif e["type"] == "click":
            spans.append((e["t"], e["t"] + 0.45))
        elif e["type"] == "camera":
            spans.append((e["t"], e["t"] + float(e.get("dur", 0.85)) + 0.35))
        elif e["type"] == "caption" and e["id"]:
            spans.append((e["t"], e["t"] + 2.0))
    spans.sort()
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


ACTIVE = activity_intervals()
SEGMENTS: list[tuple[float, float, float, float]] = []  # (src_start, src_end, out_start, rate)
QUIET: set[int] = set()  # 只为节奏压缩、不显示「加速」角标的段
out_cursor = TITLE


def push(start: float, end: float, rate: float, quiet: bool) -> None:
    global out_cursor
    if end - start <= 1e-6:
        return
    if quiet:
        QUIET.add(len(SEGMENTS))
    SEGMENTS.append((start, end, out_cursor, rate))
    out_cursor += (end - start) / rate


for index, (start, rate, quiet) in enumerate(speed_marks):
    end = speed_marks[index + 1][0] if index + 1 < len(speed_marks) else T1
    if end <= start:
        continue
    if rate != 1.0:
        push(start, end, rate, quiet)
        continue
    # 1 倍速段里找静止的空档，按 IDLE_KEEP 压缩
    cursor = start
    previous_end = start
    for a, b in [*ACTIVE, (end, end)]:
        a, b = max(a, start), min(b, end)
        if b < start or a > end:
            continue
        gap = a - previous_end
        if gap > IDLE_KEEP:
            push(cursor, previous_end, 1.0, False)
            push(previous_end, a, gap / IDLE_KEEP, True)
            cursor = a
        previous_end = max(previous_end, b)
    push(cursor, end, 1.0, False)
REC_OUT_END = out_cursor
TOTAL = REC_OUT_END + OUTRO


def to_out(src: float) -> float:
    for start, end, out_start, rate in SEGMENTS:
        if src <= end:
            return out_start + (max(src, start) - start) / rate
    return REC_OUT_END


def to_src(out: float) -> float:
    if out <= TITLE:
        return T0
    for start, end, out_start, rate in SEGMENTS:
        if out <= out_start + (end - start) / rate:
            return start + (out - out_start) * rate
    return T1


def rate_at(out: float) -> float:
    if out < TITLE or out >= REC_OUT_END:
        return 1.0
    src = to_src(out)
    for start, end, _out_start, rate in SEGMENTS:
        if start <= src <= end:
            return rate
    return 1.0


# ---------------- 镜头 ----------------
def target_of(event: dict) -> tuple[float, float, float]:
    if event.get("rect") is None:
        return FULL
    x, y, w, h = event["rect"]
    pad = event.get("pad", 1.3)
    vw = max(w * pad, h * pad * 16 / 9)
    vw = min(max(vw, max(event.get("minWidth", 760), MIN_VIEW)), WORLD_W)
    vh = vw * 9 / 16
    cx = WIN["x"] + x + w / 2
    # 字幕卡片压在画面下方约 15%：焦点内容整体往上让一点，别被字幕挡住
    cy = WIN["y"] + y + h / 2 + vh * 0.07
    cx = min(max(cx, vw / 2), WORLD_W - vw / 2)
    cy = min(max(cy, vh / 2), WORLD_H - vh / 2)
    return (cx, cy, vw)


CAMERA: list[tuple[float, float, tuple, tuple]] = []


def camera_at(t: float) -> tuple[float, float, float]:
    state = FULL
    for start, dur, frm, to in CAMERA:
        if t < start:
            break
        if t < start + dur:
            u = ease((t - start) / dur)
            return (
                frm[0] + (to[0] - frm[0]) * u,
                frm[1] + (to[1] - frm[1]) * u,
                math.exp(math.log(frm[2]) + (math.log(to[2]) - math.log(frm[2])) * u),
            )
        state = to
    return state


for event in EVENTS:
    if event["type"] != "camera":
        continue
    start = to_out(event["t"])
    CAMERA.append((start, float(event.get("dur", 1.0)), camera_at(start), target_of(event)))

# ---------------- 光标与点击 ----------------
MOVES = [e for e in EVENTS if e["type"] == "move"]
CLICKS = [(to_out(e["t"]), e["at"]) for e in EVENTS if e["type"] == "click"]


def cursor_at(src: float) -> tuple[float, float]:
    pos = (720.0, 460.0)
    for move in MOVES:
        if src < move["t"]:
            break
        u = (src - move["t"]) / max(move["dur"], 1e-3)
        fx, fy = move["from"]
        tx, ty = move["to"]
        if u < 1:
            k = ease(u)
            return (fx + (tx - fx) * k, fy + (ty - fy) * k)
        pos = (tx, ty)
    return pos


# ---------------- 字幕 ----------------
CAP_EVENTS = [(to_out(e["t"]), e["id"]) for e in EVENTS if e["type"] == "caption"]
CAP_IN, CAP_OUT = 0.5, 0.3


def caption_states(t: float) -> list[tuple[str, float, float]]:
    """当前要画的字幕：(id, 不透明度, 下移像素)。"""
    states = []
    for index, (start, cap) in enumerate(CAP_EVENTS):
        if cap is None or t < start:
            continue
        end = CAP_EVENTS[index + 1][0] if index + 1 < len(CAP_EVENTS) else REC_OUT_END
        appear = start + 0.2
        alpha = ease_out((t - appear) / CAP_IN) if t >= appear else 0.0
        offset = 18 * (1 - ease_out((t - appear) / CAP_IN)) if t >= appear else 18
        if t >= end:
            fade = (t - end) / CAP_OUT
            if fade >= 1:
                continue
            alpha *= 1 - fade
        if alpha > 0.01:
            states.append((cap, alpha, offset))
    return states


# ---------------- 资源（每个 worker 进程各自加载） ----------------
R: dict = {}


def load(name: str, mode: str = "RGBA") -> Image.Image:
    return Image.open(ASSETS / f"{name}.png").convert(mode)


def cropped(name: str) -> tuple[Image.Image, tuple[int, int]]:
    image = load(name)
    box = image.getbbox() or (0, 0, 1, 1)
    return image.crop(box), (box[0], box[1])


def init() -> None:
    R["world"] = load("world", "RGB")
    shadow = load("shadow")
    R["world_shadow"] = Image.alpha_composite(R["world"].convert("RGBA"), shadow).convert("RGB")
    R["shadow"] = shadow
    R["mask"] = load("mask", "L")
    R["cursor"] = load("cursor")
    R["layers"] = {}
    for path in ASSETS.glob("*.png"):
        if path.stem.startswith(("cap-", "speed-", "title-", "outro-")) and path.stem != "title-bg":
            R["layers"][path.stem] = cropped(path.stem)
    R["frame_index"] = -1
    R["frame"] = None


def source_frame(src: float) -> Image.Image:
    index = max(0, bisect.bisect_right(FRAME_T, src) - 1)
    if index != R["frame_index"]:
        R["frame"] = Image.open(FRAMES[index]["file"]).convert("RGB")
        R["frame_index"] = index
    return R["frame"]


def overlay(
    canvas: Image.Image, name: str, alpha: float, dy: float = 0.0, scale: float = 1.0
) -> None:
    if alpha <= 0.01:
        return
    image, (x, y) = R["layers"][name]
    if scale != 1.0:
        w, h = image.size
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        cx, cy = x + w / 2, y + h / 2
        image = image.resize((nw, nh), Image.BICUBIC)
        x, y = cx - nw / 2, cy - nh / 2
    if alpha < 1:
        a = image.getchannel("A").point(lambda v: int(v * alpha))
        image = image.copy()
        image.putalpha(a)
    canvas.alpha_composite(image, (round(x), round(y + dy)))


def ripple(canvas: Image.Image, cx: float, cy: float, u: float, s: float) -> None:
    radius = (10 + 30 * ease_out(u)) * s / 1.143
    width = 3.2 * s / 1.143
    alpha = 0.85 * (1 - u)
    size = int(radius * 2 + width * 4) + 4
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dist = np.hypot(xx - size / 2, yy - size / 2)
    ring = np.clip(width / 2 + 0.75 - np.abs(dist - radius), 0, 1) * alpha
    layer = np.zeros((size, size, 4), np.uint8)
    layer[..., 0], layer[..., 1], layer[..., 2] = 167, 72, 36
    layer[..., 3] = (ring * 255).astype(np.uint8)
    canvas.alpha_composite(Image.fromarray(layer, "RGBA"), (int(cx - size / 2), int(cy - size / 2)))


def render(t: float) -> bytes:
    cx, cy, vw = camera_at(t)
    vh = vw * 9 / 16
    vx, vy = cx - vw / 2, cy - vh / 2
    s = W / vw

    # 窗口在片头、片尾的显隐
    win_alpha = 1.0
    if t < TITLE:
        win_alpha = ease((t - (TITLE - 0.9)) / 0.9)
    elif t >= REC_OUT_END:
        win_alpha = 1 - ease((t - REC_OUT_END) / 0.8)

    box = (2 * vx, 2 * vy, 2 * (vx + vw), 2 * (vy + vh))
    if win_alpha >= 0.999:
        canvas = R["world_shadow"].resize((W, H), Image.BILINEAR, box=box).convert("RGBA")
    else:
        canvas = R["world"].resize((W, H), Image.BILINEAR, box=box).convert("RGBA")
        if win_alpha > 0.01:
            shadow = R["shadow"].resize((W, H), Image.BILINEAR, box=box)
            shadow.putalpha(shadow.getchannel("A").point(lambda v: int(v * win_alpha)))
            canvas.alpha_composite(shadow)

    src = to_src(t)
    if win_alpha > 0.01:
        x0, y0 = (WIN["x"] - vx) * s, (WIN["y"] - vy) * s
        x1, y1 = x0 + WIN["w"] * s, y0 + WIN["h"] * s
        bx0, by0 = max(0, math.floor(x0)), max(0, math.floor(y0))
        bx1, by1 = min(W, math.ceil(x1)), min(H, math.ceil(y1))
        if bx1 > bx0 and by1 > by0:

            def source_box(limit_w: float, limit_h: float) -> tuple[float, float, float, float]:
                k = limit_w / WIN["w"]
                return (
                    min(max((bx0 / s + vx - WIN["x"]) * k, 0), limit_w),
                    min(max((by0 / s + vy - WIN["y"]) * k, 0), limit_h),
                    min(max((bx1 / s + vx - WIN["x"]) * k, 0), limit_w),
                    min(max((by1 / s + vy - WIN["y"]) * k, 0), limit_h),
                )

            frame = source_frame(src)
            size = (bx1 - bx0, by1 - by0)
            region = frame.resize(
                size, Image.LANCZOS, box=source_box(*frame.size), reducing_gap=2.0
            )
            mask = R["mask"].resize(size, Image.BILINEAR, box=source_box(*R["mask"].size))
            if win_alpha < 0.999:
                mask = mask.point(lambda v: int(v * win_alpha))
            canvas.paste(region, (bx0, by0), mask)

    # 光标与点击涟漪（只在录制段）
    if TITLE - 0.3 <= t < REC_OUT_END + 0.4:
        for click_t, (px, py) in CLICKS:
            if click_t <= t < click_t + 0.5:
                ripple(
                    canvas,
                    (WIN["x"] + px - vx) * s,
                    (WIN["y"] + py - vy) * s,
                    (t - click_t) / 0.5,
                    s,
                )
        px, py = cursor_at(src)
        sx, sy = (WIN["x"] + px - vx) * s, (WIN["y"] + py - vy) * s
        size = min(30 * s * 0.9, 46)
        cursor = R["cursor"].resize((round(size), round(size)), Image.LANCZOS)
        if win_alpha < 0.999:
            cursor.putalpha(cursor.getchannel("A").point(lambda v: int(v * win_alpha)))
        canvas.alpha_composite(cursor, (int(sx - size * 5 / 30), int(sy - size * 3.1 / 30)))

    for cap, alpha, offset in caption_states(t):
        overlay(canvas, f"cap-{cap}", alpha, offset)

    rate = rate_at(t)
    badge_alpha = 0.0
    for number, (start, end, out_start, seg_rate) in enumerate(SEGMENTS):
        if seg_rate > 1 and number not in QUIET:
            out_end = out_start + (end - start) / seg_rate
            if out_start - 0.1 <= t <= out_end + 0.3:
                badge_alpha = min(ease((t - out_start) / 0.3), 1 - ease((t - out_end) / 0.3))
                rate = seg_rate
    if badge_alpha > 0:
        overlay(canvas, f"speed-{int(rate)}", badge_alpha)

    # 片头：分层错开淡入、整体淡出
    if t < TITLE:
        out_alpha = 1 - ease((t - (TITLE - 1.2)) / 0.7)
        for index, name in enumerate(["logo", "name", "tag", "pills"]):
            start = 0.25 + index * 0.32
            u = ease_out((t - start) / 0.8)
            scale = 0.92 + 0.08 * u if name == "logo" else 1.0
            overlay(canvas, f"title-{name}", u * out_alpha, 22 * (1 - u), scale)
    if t >= REC_OUT_END:
        local = t - REC_OUT_END
        for index, name in enumerate(["head", "sub", "brand"]):
            u = ease_out((local - 0.5 - index * 0.35) / 0.9)
            tail = 1 - ease((local - (OUTRO - 0.7)) / 0.7)
            overlay(canvas, f"outro-{name}", u * tail, 20 * (1 - u))
    return canvas.convert("RGB").tobytes()


def render_range(job: tuple[int, int, int]) -> str:
    index, first, last = job
    init()
    target = OUT / f"part-{index:03d}.mp4"
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{W}x{H}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    for number in range(first, last):
        encoder.stdin.write(render(number / FPS))
    encoder.stdin.close()
    encoder.wait()
    return str(target)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    total_frames = int(TOTAL * FPS)
    only = sys.argv[1:]
    if only:  # 调试：只渲染指定时刻的静帧
        init()
        for value in only:
            Image.frombytes("RGB", (W, H), render(float(value))).save(
                OUT / f"still-{value}.jpg", quality=92
            )
        return
    chunk = math.ceil(total_frames / WORKERS)
    jobs = [
        (i, i * chunk, min(total_frames, (i + 1) * chunk))
        for i in range(WORKERS)
        if i * chunk < total_frames
    ]
    with mp.get_context("fork").Pool(len(jobs)) as pool:
        parts = pool.map(render_range, jobs)
    listing = OUT / "parts.txt"
    listing.write_text("\n".join(f"file '{p}'" for p in parts))
    silent = OUT / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(silent),
        ],
        check=True,
    )
    audio = OUT / "music.wav"
    import music

    chapter_starts = [t for t, cap in CAP_EVENTS if cap and cap[-1].isdigit()]
    music.render(
        TOTAL,
        groove_from=TITLE - 0.2,
        groove_to=REC_OUT_END,
        clicks=[t for t, _ in CLICKS],
        whooshes=chapter_starts,
        path=str(audio),
    )
    final = DIR / "AgentMem-宣传片.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-af",
            "loudnorm=I=-15:TP=-1.5:LRA=11",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(final),
        ],
        check=True,
    )
    print(final, f"{TOTAL:.1f}s", total_frames, "frames")


if __name__ == "__main__":
    main()
