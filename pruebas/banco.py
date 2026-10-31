"""Banco de pruebas de fidelidad.

Genera audios con notas conocidas, los pasa por la app y mide:
  - F1 de notas: nota correcta = misma altura y empieza a menos de 50 ms
  - Similitud armónica (croma) entre el original y la guitarra resultante

Uso:  python -W ignore pruebas/banco.py [etiqueta] [opciones json]
"""
import json
import os
import sys
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mir_eval  # noqa: E402

import engine  # noqa: E402
import instrument  # noqa: E402

SR = 44100
CASES = os.path.join(HERE, "casos")
import server  # noqa: E402
PLUGIN = server.load_config()["plugin_dir"]
rng = np.random.default_rng(3)


def hz(p):
    return 440.0 * 2 ** ((p - 69) / 12)


def piano(p, d, v=0.8):
    n = int((d + 0.4) * SR)
    t = np.arange(n) / SR
    f = hz(p)
    y = np.zeros(n)
    for k in range(1, 11):
        fk = f * k * np.sqrt(1 + 1e-4 * k * k)
        if fk > SR / 2 - 1000:
            break
        y += np.sin(2 * np.pi * fk * t + rng.uniform(0, 6)) / k ** 1.2 * np.exp(-t * (0.8 + 0.35 * k))
    env = np.minimum(1, t / 0.003)
    off = int(d * SR)
    env[off:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, n - off))
    return y * env * v


def organ(p, d, v=0.6):
    n = int((d + 0.1) * SR)
    t = np.arange(n) / SR
    y = sum(np.sin(2 * np.pi * hz(p) * k * t) * a for k, a in ((1, 1), (2, .5), (3, .3), (4, .2)))
    env = np.minimum(1, t / 0.03)
    off = int(d * SR)
    env[off:] *= np.linspace(1, 0, n - off)
    return y * env * v


def voice(p, d, v=0.7):
    n = int((d + 0.1) * SR)
    t = np.arange(n) / SR
    vib = 0.3 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(1, t / 0.4)  # semitonos
    ph = 2 * np.pi * np.cumsum(hz(p) * 2 ** (vib / 12)) / SR
    y = sum(np.sin(k * ph) / k * (1.6 if 3 <= k <= 5 else 1) for k in range(1, 12) if hz(p) * k < 9000)
    env = np.minimum(1, t / 0.06)
    off = int(d * SR)
    env[off:] *= np.linspace(1, 0, n - off)
    return y * env * v


def place(buf, start, y):
    s = int(start * SR)
    e = min(len(buf), s + len(y))
    buf[s:e] += y[: e - s]


def drums(length, bpm=100):
    buf = np.zeros(int(length * SR))
    beat = 60 / bpm
    t = np.arange(int(0.4 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 * t + 70 * (1 - np.exp(-t * 30)) / 30)) * np.exp(-t * 9)
    snare = (rng.standard_normal(len(t)) * 0.6 + np.sin(2 * np.pi * 185 * t)) * np.exp(-t * 18)
    hat = np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t * 60) * 0.3
    i = 0
    while i * beat / 2 < length:
        tt = i * beat / 2
        place(buf, tt, hat)
        if i % 4 == 0:
            place(buf, tt, kick)
        if i % 4 == 2:
            place(buf, tt, snare * 0.8)
        i += 1
    return buf


ANDALUZA = [[45, 52, 57, 60, 64], [43, 50, 55, 59, 62], [41, 48, 53, 57, 60], [40, 47, 52, 56, 59, 64]]
MELODIA = [64, 65, 64, 62, 60, 59, 57, 56, 57, 59, 60, 62, 64, 65, 67, 65, 64]


def make_cases():
    os.makedirs(CASES, exist_ok=True)
    cases = {}

    # 1. piano: acordes de la cadencia andaluza + melodía frigia
    notes, buf = [], np.zeros(int(12 * SR))
    for i, ch in enumerate(ANDALUZA * 2):
        for p in ch:
            notes.append((i * 1.2, i * 1.2 + 1.1, p, 0.5))
    for i, p in enumerate(MELODIA):
        notes.append((0.6 + i * 0.55, 0.6 + i * 0.55 + 0.45, p + 12, 0.8))
    for s, e, p, v in notes:
        place(buf, s, piano(p, e - s, v))
    cases["piano_acordes"] = (buf, notes)

    # 2. órgano: acordes largos sostenidos
    notes, buf = [], np.zeros(int(11 * SR))
    for i, ch in enumerate(ANDALUZA + [[45, 52, 57, 60, 64]]):
        for p in ch:
            notes.append((i * 2.0, i * 2.0 + 1.9, p, 0.6))
    for s, e, p, v in notes:
        place(buf, s, organ(p, e - s, v / 3))
    cases["organo_sostenido"] = (buf, notes)

    # 3. voz: melodía con vibrato
    notes, buf = [], np.zeros(int(11 * SR))
    t = 0.2
    for i, p in enumerate(MELODIA[:14]):
        d = 0.6 if i % 3 else 0.9
        notes.append((t, t + d - 0.05, p, 0.7))
        t += d
    for s, e, p, v in notes:
        place(buf, s, voice(p, e - s, v))
    cases["voz_vibrato"] = (buf, notes)

    # 4. guitarra con reverb (muestras del plugin V1, otra fuente distinta)
    inst = instrument.Instrument(PLUGIN)
    gnotes = []
    t = 0.1
    for ch in ANDALUZA * 2:
        for k, p in enumerate(ch + ch[-2::-1][:3]):
            gnotes.append({"start": t, "end": t + 0.9, "pitch": p, "vel": 80 + (k == 0) * 25})
            t += 0.16
    audio = engine.render(gnotes, "v1", inst, {"reverb": 0.45}, lambda *a: None)
    cases["guitarra_reverb"] = (audio.mean(axis=1), [(n["start"], n["end"], n["pitch"], 0.7) for n in gnotes])

    # 5. piano + batería
    b, n1 = cases["piano_acordes"]
    mix = b / np.abs(b).max() * 0.7 + drums(len(b) / SR) * 0.35
    cases["piano_bateria"] = (mix, n1)

    for name, (audio, notes) in cases.items():
        audio = audio / np.abs(audio).max() * 0.8
        sf.write(os.path.join(CASES, name + ".wav"), audio.astype(np.float32), SR)
        with open(os.path.join(CASES, name + ".json"), "w") as f:
            json.dump(notes, f)


def chroma_sim(a_path, b_audio):
    import librosa
    a, _ = librosa.load(a_path, sr=22050, mono=True)
    b = librosa.resample(b_audio.mean(axis=1), orig_sr=SR, target_sr=22050)
    n = min(len(a), len(b))
    ca = librosa.feature.chroma_cqt(y=a[:n], sr=22050, hop_length=512)
    cb = librosa.feature.chroma_cqt(y=b[:n], sr=22050, hop_length=512)
    ea = librosa.feature.rms(y=a[:n], hop_length=512)[0][: ca.shape[1]]
    m = ea > ea.max() * 0.05
    num = (ca * cb).sum(0)
    den = np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0) + 1e-9
    return float((num / den)[m].mean())


def env_corr(a_path, b_audio):
    """Parecido de la dinámica: correlación de las envolventes de volumen (dB)."""
    a, _ = sf.read(a_path, dtype="float32")
    a = a if a.ndim == 1 else a.mean(1)
    b = b_audio.mean(axis=1)
    n = min(len(a), len(b))
    ea, eb = engine._env_db(a[:n], 2048, 441), engine._env_db(b[:n], 2048, 441)
    m = ea > ea.max() - 40
    return float(np.corrcoef(ea[m], eb[m])[0, 1])


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "actual"
    opts = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    if not os.path.exists(os.path.join(CASES, "piano_bateria.wav")):
        make_cases()
    inst = instrument.Instrument(PLUGIN)
    rows = []
    for name in ["piano_acordes", "organo_sostenido", "voz_vibrato", "guitarra_reverb", "piano_bateria"]:
        path = os.path.join(CASES, name + ".wav")
        with open(os.path.join(CASES, name + ".json")) as f:
            ref = json.load(f)
        t = time.time()
        notes, info = engine.analyze(path, opts, lambda *a: None)
        ri = np.array([[s, e] for s, e, _, _ in ref])
        rp = np.array([hz(p) for _, _, p, _ in ref])
        ei = np.array([[n["start"], n["end"]] for n in notes]) if notes else np.zeros((0, 2))
        ep = np.array([hz(n["pitch"]) for n in notes]) if notes else np.zeros(0)
        P, R, F, _ = mir_eval.transcription.precision_recall_f1_overlap(ri, rp, ei, ep, offset_ratio=None)
        audio = engine.render(notes, opts.get("sound", "estudio"), inst,
                              dict(opts, tuning_cents=info.get("tuning_cents", 0), ref_path=path), lambda *a: None)
        cs = chroma_sim(path, audio)
        ec = env_corr(path, audio)
        rows.append((name, len(ref), len(notes), P, R, F, cs, ec, time.time() - t))
    print(f"\n== {label} ==")
    print(f"{'caso':18} {'ref':>4} {'det':>4} {'precis':>7} {'recall':>7} {'F1':>6} {'croma':>6} {'dinám':>6} {'seg':>5}")
    for r in rows:
        print(f"{r[0]:18} {r[1]:4d} {r[2]:4d} {r[3]:7.2f} {r[4]:7.2f} {r[5]:6.2f} {r[6]:6.3f} {r[7]:6.3f} {r[8]:5.1f}")
    print(f"{'MEDIA':18} {'':4} {'':4} {np.mean([r[3] for r in rows]):7.2f} {np.mean([r[4] for r in rows]):7.2f} "
          f"{np.mean([r[5] for r in rows]):6.2f} {np.mean([r[6] for r in rows]):6.3f} {np.mean([r[7] for r in rows]):6.3f}")


if __name__ == "__main__":
    main()
