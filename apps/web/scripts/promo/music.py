"""宣传片配乐：118 BPM 的轻电子，全部用 numpy 合成，外加点击声与转场 whoosh。

结构：片头 2 小节只有铺底与琶音 → 应用画面出现时鼓组进入 → 片尾鼓组撤掉、渐弱收尾。
和弦 I–V–vi–IV（C–G–Am–F），明亮、不抢戏。
"""

from __future__ import annotations

import wave

import numpy as np

SR = 44100
BPM = 118
BEAT = 60 / BPM
BAR = BEAT * 4
CHORDS = [  # (根音 MIDI, 和弦音)
    (48, [60, 64, 67, 71]),  # Cmaj7
    (43, [59, 62, 67, 71]),  # G
    (45, [57, 60, 64, 67]),  # Am7
    (41, [57, 60, 65, 69]),  # Fmaj7
]


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def place(
    track: np.ndarray, start: float, sound: np.ndarray, gain: float = 1.0, pan: float = 0.0
) -> None:
    i = int(start * SR)
    if i >= track.shape[0] or i + len(sound) <= 0:
        return
    sound = sound[: track.shape[0] - i]
    left = np.sqrt(0.5 * (1 - pan))
    right = np.sqrt(0.5 * (1 + pan))
    track[i : i + len(sound), 0] += sound * gain * left * 1.414
    track[i : i + len(sound), 1] += sound * gain * right * 1.414


def saw(freq: float, n: int, harmonics: int = 12, bright: float = 1.0) -> np.ndarray:
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, harmonics + 1):
        if freq * k > 14000:
            break
        out += np.sin(2 * np.pi * freq * k * t) / k * np.exp(-(k - 1) * (1.2 - bright) * 0.6)
    return out


def filtered_noise(n: int, low: float, high: float, rng: np.random.Generator) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    spectrum[(freqs < low) | (freqs > high)] = 0
    out = np.fft.irfft(spectrum, n)
    return out / (np.max(np.abs(out)) + 1e-9)


def kick() -> np.ndarray:
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    freq = 46 + 110 * np.exp(-t * 28)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 7.5)
    click = np.exp(-t * 400) * 0.35
    return body + click


def clap(rng: np.random.Generator) -> np.ndarray:
    n = int(0.28 * SR)
    t = np.arange(n) / SR
    noise = filtered_noise(n, 900, 6000, rng)
    env = (
        np.exp(-t * 22)
        + 0.5 * np.exp(-((t - 0.012) ** 2) / 1e-5)
        + 0.4 * np.exp(-((t - 0.024) ** 2) / 1e-5)
    )
    return noise * env


def hat(rng: np.random.Generator, open_: bool = False) -> np.ndarray:
    n = int((0.16 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    return filtered_noise(n, 7000, 16000, rng) * np.exp(-t * (18 if open_ else 70))


def pluck(midi: float, dur: float = 0.32) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(midi)
    tone = 0.45 * np.sin(2 * np.pi * f * t) + 0.55 * saw(f, n, harmonics=16, bright=0.9)
    return tone * np.exp(-t * 12) * np.minimum(1, t / 0.003)


def bass(midi: float, dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(midi)
    tone = 0.75 * np.sin(2 * np.pi * f * t) + 0.35 * saw(f, n, harmonics=5, bright=0.3)
    env = np.minimum(1, t / 0.006) * np.exp(-t * 3.5) * np.minimum(1, (dur - t) / 0.03)
    return tone * env


def pad(notes: list[int], dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for i, midi in enumerate(notes):
        for detune in (-0.08, 0.08):
            out += saw(hz(midi + detune), n, harmonics=9, bright=0.5) * (0.9 if i else 1.0)
    env = np.minimum(1, t / 0.35) * np.minimum(1, (dur - t) / 0.4)
    return out * env / len(notes)


def bell(midi: float) -> np.ndarray:
    n = int(1.4 * SR)
    t = np.arange(n) / SR
    f = hz(midi)
    tone = (
        np.sin(2 * np.pi * f * t)
        + 0.5 * np.sin(2 * np.pi * f * 2.76 * t) * np.exp(-t * 6)
        + 0.25 * np.sin(2 * np.pi * f * 5.4 * t) * np.exp(-t * 9)
    )
    return tone * np.exp(-t * 3.2) * np.minimum(1, t / 0.002)


def tick() -> np.ndarray:
    n = int(0.05 * SR)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * 2400 * t) * 0.6 + np.sin(2 * np.pi * 1200 * t) * 0.4) * np.exp(
        -t * 130
    )


def whoosh(rng: np.random.Generator) -> np.ndarray:
    n = int(0.7 * SR)
    t = np.arange(n) / SR
    rng.standard_normal(n)
    # 频带随时间上扫：分段带通后拼接，近似一个扫频滤波
    out = np.zeros(n)
    pieces = 14
    size = n // pieces
    for k in range(pieces):
        low = 300 + 3500 * (k / pieces) ** 1.4
        seg = filtered_noise(size * 3, low, low * 2.2, rng)[size : size * 2]
        out[k * size : (k + 1) * size] = seg
    env = np.sin(np.pi * np.minimum(1, t / 0.7)) ** 2
    return out * env * 0.5


def render(
    duration: float,
    *,
    groove_from: float,
    groove_to: float,
    clicks: list[float],
    whooshes: list[float],
    path: str,
) -> None:
    rng = np.random.default_rng(7)
    n = int(duration * SR) + SR
    music = np.zeros((n, 2))
    drums = np.zeros((n, 2))
    pads = np.zeros((n, 2))
    k, c, hc, ho = kick(), clap(rng), hat(rng), hat(rng, open_=True)

    bars = int(duration / BAR) + 1
    duck = np.ones(n)  # 侧链：鼓点处把铺底压下去，形成律动
    for b in range(bars):
        bar_t = b * BAR
        root, notes = CHORDS[b % 4]
        place(pads, bar_t, pad(notes, BAR + 0.3), 0.30)
        in_groove = groove_from - 0.05 <= bar_t < groove_to
        # 琶音：16 分音符在和弦音之间上下走，带乒乓延迟
        arp = [*notes, notes[1] + 12, notes[2] + 12]
        order = [0, 2, 1, 3, 2, 4, 3, 5, 4, 3, 2, 1, 2, 3, 4, 5]
        for i, idx in enumerate(order):
            when = bar_t + i * BEAT / 4
            gain = 0.19 if in_groove else 0.12
            note = pluck(arp[idx % len(arp)] + 12)
            place(music, when, note, gain, pan=-0.35 if i % 2 else 0.35)
            place(music, when + BEAT * 0.75, note, gain * 0.35, pan=0.6 if i % 2 else -0.6)
        # 每个和弦换的时候一声铃音，给画面一点亮度
        place(music, bar_t, bell(notes[-1] + 24), 0.10 if in_groove else 0.07, pan=0.2)
        if not in_groove:
            continue
        for beat in range(4):
            when = bar_t + beat * BEAT
            place(drums, when, k, 0.85)
            i0 = int(when * SR)
            span = int(0.22 * SR)
            if i0 < n:
                shape = 1 - 0.65 * np.exp(-np.arange(min(span, n - i0)) / SR * 14)
                duck[i0 : i0 + len(shape)] = np.minimum(duck[i0 : i0 + len(shape)], shape)
            if beat in (1, 3):
                place(drums, when, c, 0.5)
            place(drums, when + BEAT / 2, ho if beat == 3 else hc, 0.7, pan=0.25)
            place(drums, when + BEAT / 4, hc, 0.3, pan=-0.3)
            place(drums, when + 3 * BEAT / 4, hc, 0.3, pan=-0.3)
            # 贝斯：反拍跳动的八分音符
            place(music, when + BEAT / 2, bass(root, BEAT / 2 * 0.9), 0.3)
            if beat == 0:
                place(music, when, bass(root, BEAT / 2 * 0.9), 0.22)
    pads *= duck[:, None]
    mix = music + drums + pads
    for t in clicks:
        place(mix, t, tick(), 0.18)
    for t in whooshes:
        place(mix, t - 0.35, whoosh(rng), 0.22)

    mix = mix[: int(duration * SR)]
    # 母线高架：2.5 kHz 以上提约 8 dB，加法合成的音色天生偏闷
    freqs = np.fft.rfftfreq(mix.shape[0], 1 / SR)
    shelf = 1 + 1.5 / (1 + np.exp(-(freqs - 2500) / 600))
    for ch in range(2):
        mix[:, ch] = np.fft.irfft(np.fft.rfft(mix[:, ch]) * shelf, mix.shape[0])
    t = np.arange(mix.shape[0]) / SR
    fade = np.minimum(1, t / 1.2) * np.minimum(1, (duration - t) / 2.5)
    mix *= fade[:, None]
    mix = np.tanh(mix * 1.1) / np.tanh(1.1)
    mix *= 0.82 / (np.max(np.abs(mix)) + 1e-9)
    data = (mix * 32767).astype(np.int16)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(data.tobytes())
