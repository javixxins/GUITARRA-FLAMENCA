"""Banco de pruebas de la detección de acordes.

Genera canciones con acordes conocidos (guitarra con muestras reales del plugin rasgueada,
piano, órgano, bajo, melodía, batería y palmas; varias tonalidades, modos y compases) y mide
con mir_eval (las métricas oficiales de MIREX):
  - root: aciertos de la fundamental
  - majmin: fundamental + mayor/menor
  - sevenths: también el tipo de séptima
Compara con el método «sencillo» (croma CQT + plantillas + coseno por fotograma).

Uso:  python -W ignore pruebas/acordes_banco.py
"""
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import mir_eval

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import engine  # noqa: E402
import instrument  # noqa: E402
import acordes  # noqa: E402
import server  # noqa: E402

SR = 44100
OUT = os.path.join(HERE, "casos_acordes")
rng = np.random.default_rng(11)
PC = {n: i for i, n in enumerate(acordes.NOTE_SHARP)}
PC.update({"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10})


def parse(ch):
    r, q = ch.split(":")
    return PC[r], q


def chord_pcs(ch):
    r, q = parse(ch)
    return [(r + i) % 12 for i in acordes.QUALITIES[q][0]]


def hz(p):
    return 440.0 * 2 ** ((p - 69) / 12)


def place(buf, t, y):
    s = int(t * SR)
    e = min(len(buf), s + len(y))
    if e > s:
        buf[s:e] += y[: e - s]


def piano(p, d, v=0.5):
    n = int((d + 0.3) * SR)
    t = np.arange(n) / SR
    y = sum(np.sin(2 * np.pi * hz(p) * k * t + rng.uniform(0, 6)) / k ** 1.3 * np.exp(-t * (0.9 + 0.4 * k))
            for k in range(1, 9) if hz(p) * k < 10000)
    env = np.minimum(1, t / 0.004)
    off = int(d * SR)
    env[off:] *= np.linspace(1, 0, n - off)
    return y * env * v


def organ(p, d, v=0.3):
    n = int((d + 0.05) * SR)
    t = np.arange(n) / SR
    y = sum(np.sin(2 * np.pi * hz(p) * k * t) * a for k, a in ((1, 1), (2, .5), (3, .25), (4, .2)))
    env = np.minimum(1, t / 0.04) * np.minimum(1, (n - np.arange(n)) / (0.05 * SR))
    return y * env * v


def bassline(p, d, v=0.6):
    n = int(d * SR)
    t = np.arange(n) / SR
    y = sum(np.sin(2 * np.pi * hz(p) * k * t) / k ** 1.5 for k in range(1, 6))
    return y * np.minimum(1, t / 0.01) * np.exp(-t * 1.2) * np.minimum(1, (n - np.arange(n)) / (0.03 * SR)) * v


def voice(p, d, v=0.35):
    n = int(d * SR)
    t = np.arange(n) / SR
    vib = 0.25 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(1, t / 0.3)
    ph = 2 * np.pi * np.cumsum(hz(p) * 2 ** (vib / 12)) / SR
    y = sum(np.sin(k * ph) / k for k in range(1, 10) if hz(p) * k < 9000)
    return y * np.minimum(1, t / 0.05) * np.minimum(1, (n - np.arange(n)) / (0.05 * SR)) * v


def drums(length, beat):
    buf = np.zeros(int(length * SR))
    t = np.arange(int(0.3 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 * t + 70 * (1 - np.exp(-t * 30)) / 30)) * np.exp(-t * 9)
    snare = (rng.standard_normal(len(t)) * 0.6 + np.sin(2 * np.pi * 185 * t)) * np.exp(-t * 18)
    hat = np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t * 60) * 0.3
    i = 0
    while i * beat / 2 < length:
        place(buf, i * beat / 2, hat)
        if i % 4 == 0:
            place(buf, i * beat / 2, kick)
        if i % 4 == 2:
            place(buf, i * beat / 2, snare * 0.8)
        i += 1
    return buf


def palmas(length, beat, accents):
    buf = np.zeros(int(length * SR))
    t = np.arange(int(0.12 * SR)) / SR
    clap = np.convolve(rng.standard_normal(len(t)), [1, -0.95], "same") * np.exp(-t * 45)
    k = 0
    while k * beat < length:
        place(buf, k * beat, clap * (0.9 if (k % len(accents)) in accents else 0.35))
        k += 1
    return buf


def guitar_voicing(ch):
    """Una postura de guitarra genérica: fundamental grave y notas del acorde por encima."""
    r, q = parse(ch)
    pcs = chord_pcs(ch)
    root = 40 + (r - 4) % 12
    if root > 47:
        root -= 12 if root - 12 >= 40 else 0
    notes = [root]
    p = root + 1
    order = [pcs[2 % len(pcs)], pcs[0], pcs[1], pcs[2 % len(pcs)], pcs[-1] if len(pcs) > 3 else pcs[0]]
    for pc in order:
        while p % 12 != pc:
            p += 1
        notes.append(p)
        p += 1
    return [n for n in notes if n <= 76]


def song(name, prog, beats_per_chord, bpm, layers, reps=3, meter=4):
    beat = 60.0 / bpm
    chords = prog * reps
    length = len(chords) * beats_per_chord * beat + 2.0
    mix = np.zeros(int(length * SR))
    ref = []
    gnotes = []
    t = 0.3
    for ch in chords:
        d = beats_per_chord * beat
        ref.append((t, t + d, ch))
        pcs = chord_pcs(ch)
        r = parse(ch)[0]
        if "piano" in layers:
            for k in range(beats_per_chord // 2 or 1):
                tt = t + k * 2 * beat
                for p in [48 + (pc - 0) % 12 for pc in pcs] + [60 + pcs[0]]:
                    place(mix, tt, piano(p, 2 * beat * 0.95, 0.35))
        if "organ" in layers:
            for p in [52 + (pc - 4) % 12 for pc in pcs]:
                place(mix, t, organ(p, d))
        if "bass" in layers:
            for k in range(beats_per_chord):
                p = 36 + (r - 0) % 12 if k % 2 == 0 else 36 + (pcs[2 % len(pcs)]) % 12
                place(mix, t + k * beat, bassline(p, beat * 0.9))
        if "guitar" in layers:
            v = guitar_voicing(ch)
            strokes = range(beats_per_chord) if "rasgueo" in layers else range(0, beats_per_chord, 2)
            for k in strokes:
                for p in v:
                    gnotes.append({"start": t + k * beat, "end": t + (k + 1) * beat, "pitch": p, "vel": 95 if k % meter == 0 else 75})
        if "melody" in layers:
            for k in range(beats_per_chord):
                pc = pcs[(k + len(ref)) % len(pcs)] if k % 2 == 0 else (pcs[0] + [2, 5, 9][k % 3]) % 12
                place(mix, t + k * beat, voice(64 + (pc - 4) % 12, beat * 0.95))
        t += d
    if gnotes:
        inst = instrument.Instrument(server.load_config()["plugin_dir"])
        g = engine.render(gnotes, "estudio", inst, {"strum": 0.6, "tremolo": "off", "reverb": 0.2}, lambda *a: None, dry=True)
        g = g.mean(axis=1)
        g = g / (np.abs(g).max() + 1e-9) * 0.6
        n = min(len(g), len(mix))
        mix[:n] += g[:n] * (1.6 if len(layers) <= 2 else 1.0)
    t0 = int(0.3 * SR)  # la percusión empieza con los acordes
    if "drums" in layers:
        mix[t0:] += drums(length, beat)[: len(mix) - t0] * 0.3 * np.abs(mix).max()
    if "palmas" in layers:
        mix[t0:] += palmas(length, beat, (2, 5, 7, 9, 11) if meter == 12 else (0,))[: len(mix) - t0] * 0.25 * np.abs(mix).max()
    mix = mix / np.abs(mix).max() * 0.85
    return mix, ref


SONGS = [
    ("andaluza_guitarra", ["A:min", "G:maj", "F:maj", "E:maj"], 4, 100, ["guitar", "rasgueo"]),
    ("por_arriba_palmas", ["E:maj", "F:maj", "G:maj", "F:maj", "E:maj", "A:min", "G:maj", "F:maj"], 3, 132, ["guitar", "rasgueo", "palmas"]),
    ("por_medio_voz", ["D:min", "C:maj", "A#:maj", "A:maj"], 4, 96, ["guitar", "melody", "bass"]),
    ("pop_piano_C", ["C:maj", "G:maj", "A:min", "F:maj"], 4, 90, ["piano", "bass", "melody"]),
    ("septimas_Bb", ["C:min7", "F:7", "A#:maj7", "G:min7", "C:min7", "F:7", "A#:maj7", "D:7"], 2, 110, ["piano", "bass"]),
    ("rumba_D", ["D:maj", "A:maj", "B:min", "G:maj"], 4, 124, ["organ", "bass", "drums", "melody", "guitar"]),
    ("menor_lenta_Fs", ["F#:min", "D:maj", "A:maj", "E:maj"], 4, 70, ["guitar", "melody"]),
    ("sevillanas_E", ["E:maj", "A:maj", "B:7", "E:maj", "A:maj", "E:maj", "B:7", "E:maj"], 3, 150, ["guitar", "rasgueo", "palmas"]),
    ("tangos_Am7", ["A:min7", "D:min7", "E:7", "A:min", "F:maj7", "E:7"], 4, 112, ["guitar", "bass", "drums"]),
]


# canciones nuevas que NO se usan para ajustar el detector: miden la fiabilidad real
SONGS_TEST = [
    ("t_bulerias_A", ["A:maj", "A#:maj", "C:maj", "A#:maj", "A:maj", "D:min", "C:maj", "A#:maj"], 3, 168, ["guitar", "rasgueo", "palmas"]),
    ("t_soleares_E", ["A:min", "G:maj", "F:maj", "E:maj", "A:min", "D:min", "E:7", "A:min"], 3, 72, ["guitar", "rasgueo", "melody"]),
    ("t_pop_G", ["G:maj", "D:maj", "E:min", "C:maj"], 4, 100, ["piano", "bass", "drums"]),
    ("t_jazz_F", ["G:min7", "C:7", "F:maj7", "D:min7", "G:min7", "C:7", "F:maj7", "A:7"], 2, 120, ["piano", "bass"]),
    ("t_Eb_organo", ["D#:maj", "G#:maj", "A#:maj", "C:min"], 4, 84, ["organ", "melody", "bass"]),
    ("t_taranta_Fs", ["B:min", "A:maj", "G:maj", "F#:maj", "G:maj", "F#:maj"], 4, 116, ["guitar", "bass", "drums"]),
    ("t_menor_Cm", ["C:min", "G#:maj", "D#:maj", "A#:maj"], 4, 88, ["guitar", "rasgueo", "melody"]),
]


def simple_method(path):
    """Método sencillo (el del ejemplo): croma CQT + plantillas mayor/menor + coseno por fotograma."""
    import librosa
    y, sr = librosa.load(path, sr=22050)
    hop = 2048
    ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    times = librosa.frames_to_time(np.arange(ch.shape[1] + 1), sr=sr, hop_length=hop)
    labels = []
    for i in range(ch.shape[1]):
        best, bs = "N", 0.65
        for r in range(12):
            for q in ("maj", "min"):
                t = np.zeros(12)
                for iv in acordes.QUALITIES[q][0]:
                    t[(r + iv) % 12] = 1
                c = ch[:, i] @ t / (np.linalg.norm(ch[:, i]) * np.linalg.norm(t) + 1e-9)
                if c > bs:
                    best, bs = f"{acordes.NOTE_SHARP[r]}:{q}", c
        labels.append(best)
    return np.stack([times[:-1], times[1:]], 1), labels


def evaluate(ref, est_int, est_lab):
    ri = np.array([[a, b] for a, b, _ in ref])
    rl = [c for _, _, c in ref]
    ei, el = mir_eval.util.adjust_intervals(np.asarray(est_int), est_lab, ri.min(), ri.max(),
                                            mir_eval.chord.NO_CHORD, mir_eval.chord.NO_CHORD)
    ei, el = mir_eval.util.adjust_intervals(ei, el, ri.min(), ri.max(), mir_eval.chord.NO_CHORD, mir_eval.chord.NO_CHORD)
    merged, rl2, el2 = mir_eval.util.merge_labeled_intervals(ri, rl, ei, el)
    w = mir_eval.util.intervals_to_durations(merged)
    out = {}
    for m in ("root", "majmin", "sevenths"):
        comp = getattr(mir_eval.chord, m)(rl2, el2)
        out[m] = mir_eval.chord.weighted_accuracy(comp, w)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    songs = SONGS_TEST if "--nuevas" in sys.argv else SONGS
    for name, prog, bpc, bpm, layers in songs:
        p = os.path.join(OUT, name + ".wav")
        meter = 12 if "palmas" in layers and bpc == 3 else 4
        if not os.path.exists(p):
            mix, ref = song(name, prog, bpc, bpm, layers, meter=meter)
            sf.write(p, mix.astype(np.float32), SR)
            with open(p[:-4] + ".json", "w") as f:
                json.dump(ref, f)
        with open(p[:-4] + ".json") as f:
            ref = json.load(f)
        t = time.time()
        prep = engine.prepare(p, {"remove_percussion": True})
        res = acordes.detect(p, prep)
        dt = time.time() - t
        segs = res["segments"]
        mine = evaluate(ref, [[s["start"], s["end"]] for s in segs], [s["label"] for s in segs])
        si, sl = simple_method(p)
        simple = evaluate(ref, si, sl)
        rows.append((name, mine, simple, res, dt))
        print(f"{name:20} raíz {mine['root']:.2f}  mayor/menor {mine['majmin']:.2f}  séptimas {mine['sevenths']:.2f}"
              f"  | sencillo: {simple['majmin']:.2f} | {res['key']['name']}, {res['tempo']:.0f} BPM, "
              f"{res['meter']}/4, fiab. {res['reliability']:.2f}, {dt:.1f} s", flush=True)
    print(f"\n{'MEDIA':20} raíz {np.mean([r[1]['root'] for r in rows]):.3f}  mayor/menor "
          f"{np.mean([r[1]['majmin'] for r in rows]):.3f}  séptimas {np.mean([r[1]['sevenths'] for r in rows]):.3f}"
          f"  | sencillo mayor/menor {np.mean([r[2]['majmin'] for r in rows]):.3f}")


if __name__ == "__main__":
    main()
