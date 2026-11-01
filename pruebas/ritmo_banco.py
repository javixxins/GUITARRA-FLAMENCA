"""Banco de pruebas de «copiar el ritmo del original».

Fabrica originales con golpes conocidos (riffs de guitarra del plugin y de piano, con batería o
palmas encima), genera la guitarra rítmica con la app y mide con mir_eval:
  - golpes encontrados en el original (F1 de ataques, margen 50 ms)
  - golpes de la pista generada frente a los del original (F1, margen 50 ms)
  - acentos (correlación de la fuerza de cada golpe)
  - tipo de golpe (rasgueo / pulgar / notas sueltas / apagado / rasgueado)

Uso:  python -W ignore pruebas/ritmo_banco.py
"""
import os
import sys
import time

import numpy as np
import soundfile as sf
import librosa
import mir_eval

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import engine  # noqa: E402
import instrument  # noqa: E402
import server  # noqa: E402
import acordes  # noqa: E402
import guitarra_ritmica as g  # noqa: E402
import ritmo_original  # noqa: E402
from acordes_banco import drums, palmas, piano, bassline, chord_pcs  # noqa: E402

SR = 44100
OUT = os.path.join(HERE, "casos_ritmo")
FAMILY = {"D": "rasgueo", "U": "rasgueo", "R": "rasgueado", "X": "apagado", "P": "pulgar", "T": "pulgar",
          "N": "notas", "H": "notas", "S1": "notas", "S2": "notas", "S3": "notas"}

RIFFS = [
    ("rumba_sincopada", 112, ["A:min", "G:maj", "F:maj", "E:maj"], 4,
     [(0, "D", 1.0), (0.75, "U", 0.5), (1.5, "X", 0.9), (2, "D", 0.8), (2.75, "U", 0.5), (3.5, "X", 0.9)], "drums"),
    ("pulgar_pinzas", 90, ["E:maj", "F:maj", "G:maj", "F:maj"], 4,
     [(0, "P", 1.0), (0.5, "H", 0.6), (1, "P", 0.8), (1.5, "H", 0.6), (2, "P", 0.9), (2.5, "H", 0.6), (3, "P", 0.8), (3.5, "H", 0.7)], None),
    ("bulerias_acentos", 190, ["A:maj", "A#:maj", "C:maj", "A#:maj"], 12,
     [(b, "R" if b in (2, 7) else "D", 1.0 if b in (2, 5, 7, 9, 11) else 0.4) for b in (0, 2, 4, 5, 7, 9, 11)], "palmas"),
    ("arpegio_melodico", 100, ["D:min", "C:maj", "A#:maj", "A:maj"], 4,
     [(0, "P", 1.0), (0.5, "S3", 0.6), (1, "S2", 0.6), (1.5, "S1", 0.7), (2, "P", 0.9), (2.5, "S2", 0.6), (3, "S1", 0.7), (3.5, "S3", 0.5)], None),
    ("piano_sincopado", 96, ["C:maj", "G:maj", "A:min", "F:maj"], 4, "piano", "drums"),
]


def make(name, bpm, prog, meter, strokes, perc, bars=8):
    beat = 60.0 / bpm
    t0 = 0.4
    gt = []
    if strokes == "piano":
        length = bars * 4 * beat + t0 + 2
        mix = np.zeros(int(length * SR))
        for b in range(bars):
            ch = prog[b % len(prog)]
            for pos, acc in ((0, 1.0), (1.5, 0.6), (2.5, 0.8), (3.5, 0.5)):
                t = t0 + (b * 4 + pos) * beat
                for pc in chord_pcs(ch):
                    y = piano(48 + pc, beat * 0.45, 0.35 * acc)
                    s = int(t * SR)
                    mix[s:s + len(y)] += y[: len(mix) - s]
                gt.append((t, "rasgueo", acc))
            gt.append((t0 + (b * 4 + 2) * beat, "pulgar", 0.6))   # el bajo del tiempo 3 también es un golpe
            r = chord_pcs(ch)[0]
            for pos in (0, 2):
                y = bassline(36 + r, beat * 0.9)
                s = int((t0 + (b * 4 + pos) * beat) * SR)
                mix[s:s + len(y)] += y[: len(mix) - s]
    else:
        per_bar = meter * beat
        length = bars * per_bar + t0 + 2
        segs, labels = [], []
        for b in range(bars):
            segs.append({"start": t0 + b * per_bar, "end": t0 + (b + 1) * per_bar, "label": prog[b % len(prog)]})
            labels.append(prog[b % len(prog)])
        vo = g.voice_lead(labels, 0, "natural")
        rng = np.random.default_rng(1)
        p = {"speed": 0.012, "vel": 100}
        hits = []
        for b in range(bars):
            for pos, kind, acc in strokes:
                t = t0 + b * per_bar + pos * beat
                for s, tt, n, v, short in g.strike(kind, t, vo[b], acc, p, rng, beat):
                    hits.append((s, tt, n, v, short))
                gt.append((t, FAMILY[kind], acc))
        hits.sort(key=lambda h: h[1])
        notes = []
        for k, (s, t, n, v, short) in enumerate(hits):
            nxt = next((h[1] for h in hits[k + 1:] if h[0] == s), t + 1.5)
            end = min(nxt, t + 0.05) if short else nxt
            notes.append({"start": t, "end": max(end, t + 0.04), "pitch": n, "vel": int(min(127, v)),
                          "release": 0.03 if short else 0.06})
        inst = instrument.Instrument(server.load_config()["plugin_dir"])
        a = engine.render(notes, "escenario", inst, {"strum": 0, "tremolo": "off", "guitar_register": False, "reverb": 0.15},
                          lambda *x: None, dry=True).mean(axis=1)
        mix = np.zeros(int(length * SR))
        n = min(len(a), len(mix))
        mix[:n] += a[:n]
    peak = np.abs(mix).max()
    s0 = int(t0 * SR)
    if perc == "drums":
        mix[s0:] += drums(length, beat)[: len(mix) - s0] * 0.35 * peak
    elif perc == "palmas":
        mix[s0:] += palmas(length, beat, (2, 5, 7, 9, 11))[: len(mix) - s0] * 0.3 * peak
    mix = mix / np.abs(mix).max() * 0.85
    return mix, gt


def onsets(y, sr):
    o = librosa.onset.onset_detect(y=y, sr=sr, units="time", hop_length=256, backtrack=False)
    return cluster(np.asarray(o))


def cluster(ts, gap=0.075):
    out = []
    for t in sorted(ts):
        if not out or t - out[-1] > gap:
            out.append(t)
    return np.array(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    inst = instrument.Instrument(server.load_config()["plugin_dir"])
    rows = []
    for name, bpm, prog, meter, strokes, perc in RIFFS:
        p = os.path.join(OUT, name + ".wav")
        mix, gt = make(name, bpm, prog, meter, strokes, perc)
        sf.write(p, mix.astype(np.float32), SR)
        gt = sorted(gt)
        gt_t = cluster([t for t, _, _ in gt])
        t = time.time()
        prep = engine.prepare(p, {"remove_percussion": True})
        res = acordes.detect(p, prep)
        ev = ritmo_original.extract(prep, res["beats"])
        segs = res["segments"]
        capo = g.choose_capo([(s["label"], s["end"] - s["start"]) for s in segs if s["label"] != "N"])
        vo = g.voice_lead([s["label"] for s in segs], capo)
        if "--patron" in sys.argv:   # comparación: un patrón fijo en vez de copiar el original
            notes = g.generate(segs, vo, res["beats"], res["downbeats"], {"pattern": sys.argv[sys.argv.index("--patron") + 1]})
        else:
            notes = g.generate_from_original(segs, vo, ev, {})
        out = engine.render(notes, "estudio", inst, {"strum": 0, "tremolo": "off", "guitar_register": False,
                            "reverb": 0.1, "copy_timbre": 0, "follow_dynamics": 0}, lambda *x: None)
        dt = time.time() - t
        sf.write(os.path.join(OUT, name + "_ritmica.wav"), out, SR)
        ev_t = cluster([e["t"] for e in ev])
        f_ext = mir_eval.onset.f_measure(gt_t, ev_t, window=0.05)[0]
        gen_t = onsets(out.mean(axis=1), SR)
        f_gen = mir_eval.onset.f_measure(gt_t, gen_t, window=0.05)[0]
        # acentos y tipo de golpe en los golpes emparejados
        m = mir_eval.util.match_events(np.array([x[0] for x in gt]), np.array([e["t"] for e in ev]), 0.05)
        acc_r = np.corrcoef([gt[i][2] for i, _ in m], [ev[j]["acc"] for _, j in m])[0, 1] if len(m) > 3 else np.nan
        kinds = np.mean([gt[i][1] == FAMILY.get(ev[j]["kind"], "?") for i, j in m]) if m else 0
        if "--confusion" in sys.argv:
            from collections import Counter
            print("   ", dict(Counter(f"{gt[i][1]}->{FAMILY.get(ev[j]['kind'])}" for i, j in m)))
        rows.append((f_ext, f_gen, acc_r, kinds))
        print(f"{name:18} golpes del original encontrados F1 {f_ext:.2f} · pista generada F1 {f_gen:.2f} · "
              f"acentos r={acc_r:.2f} · tipo de golpe {kinds:.0%} · {len(ev)} golpes, {dt:.1f} s", flush=True)
    r = np.array(rows, float)
    print(f"\nMEDIA  golpes originales F1 {np.nanmean(r[:, 0]):.3f} · pista generada F1 {np.nanmean(r[:, 1]):.3f} · "
          f"acentos r {np.nanmean(r[:, 2]):.2f} · tipo de golpe {np.nanmean(r[:, 3]):.0%}")


if __name__ == "__main__":
    main()
