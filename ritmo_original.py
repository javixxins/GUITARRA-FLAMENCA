"""Copia el ritmo (el riff) del original para la guitarra rítmica.

1. Golpes: ataques del audio sin percusión (flujo espectral tipo «superflux», que ignora el
   vibrato) + los comienzos de nota que oye basic-pitch. Así se siguen los golpes de los
   instrumentos con notas, no los de la batería o las palmas.
2. Cada golpe se describe con lo que suena en él: qué notas (graves, agudas, cuántas), con qué
   fuerza (acento) y cuánto duran (ligado o apagado).
3. Se traduce a técnica de guitarra flamenca:
     - varios golpes muy seguidos (< 75 ms)          -> rasgueado de dedos (R)
     - acorde lleno con bajo, o en la parte fuerte    -> rasgueo hacia abajo (D)
     - acorde sin bajo, flojo y a contratiempo        -> rasgueo hacia arriba (U)
     - acorde que se corta enseguida                  -> apagado (X)
     - una nota grave sola                            -> pulgar en el bajo (P)
     - una o dos notas agudas                         -> las mismas notas punteadas (N), para
                                                         que el riff conserve su melodía
"""
import numpy as np
import librosa

import engine
from basic_pitch import note_creation as infer

SR = 22050


def _onset_functions(prep, hop):
    """Dos funciones de ataque que sólo reaccionan a sonidos con altura (no a la batería):
    el flujo del espectro CQT (hasta ~4 kHz) y los «comienzos de nota» de basic-pitch."""
    y = prep.audio
    C = np.abs(librosa.cqt(y, sr=SR, hop_length=hop, fmin=librosa.midi_to_hz(28), n_bins=72, bins_per_octave=12,
                           tuning=float(prep.tuning)))
    L = librosa.amplitude_to_db(C, ref=np.max)
    flux = np.maximum(0, np.diff(L, axis=1, prepend=L[:, :1]))
    fA = np.convolve(flux.mean(axis=0), [0.25, 0.5, 0.25], mode="same")
    tA = librosa.frames_to_time(np.arange(len(fA)), sr=SR, hop_length=hop)
    on = prep.output["onset"]                                  # fotogramas x 88 notas
    srt = np.sort(on, axis=1)[:, -6:].sum(axis=1)               # las 6 notas que más «empiezan»
    tB = infer.model_frames_to_time(len(srt))
    fB = np.interp(tA, tB, srt)
    # qué notas se pulsan en cada instante (aunque ya estuvieran sonando: re-rasgueos)
    idx = np.clip(np.searchsorted(tB, tA), 0, len(tB) - 1)
    onA = on[idx].T                                             # 88 x fotogramas (MIDI 21-108)
    P = C ** 2
    bands = (P[:57 - 28].sum(axis=0), P[64 - 28:].sum(axis=0))   # graves (≤ La2) y agudas (≥ Mi4)
    return tA, fA / (np.percentile(fA, 99) + 1e-9), fB / (np.percentile(fB, 99) + 1e-9), onA, flux, bands


def _struck(onA, flux, f):
    """Notas que se pulsan en el fotograma f: probabilidad de comienzo de basic-pitch y, si no
    basta, subida de energía de cada semitono en el espectro."""
    a, b = max(0, f - 2), f + 5
    pb = onA[:, a:b].max(axis=1)
    ps = [21 + i for i in np.nonzero(pb > 0.25)[0]]
    if len(ps) < 2:
        fl = flux[:, a:b].max(axis=1)
        if fl.max() > 3:
            ps = sorted(set(ps) | {28 + i for i in np.nonzero(fl > 0.45 * fl.max())[0]})
    return ps


def extract(prep, beats=None, notes=None):
    """Devuelve la lista de golpes del original: [{t, kind, acc, pitches, dur}]."""
    y = prep.audio
    hop = 256
    tA, fA, fB, onA, flux, (lowb, highb) = _onset_functions(prep, hop)

    def band_jump(b, f):                           # cuántos dB sube una banda en el golpe
        return 10 * np.log10(b[f + 1:f + 6].max() + 1e-12) - 10 * np.log10(b[max(0, f - 6):max(1, f - 1)].mean() + 1e-12)
    comb = 0.5 * fA + 0.5 * fB
    peaks = librosa.util.peak_pick(comb, pre_max=3, post_max=3, pre_avg=12, post_avg=6, delta=0.08, wait=4)
    if notes is None:
        notes, _ = engine.notes_from(prep, {"sensitivity": 0.5, "clean": True})
    rms = librosa.feature.rms(y=y, frame_length=512, hop_length=hop)[0]
    rms_db = librosa.amplitude_to_db(rms + 1e-9, ref=np.max)

    cand = []
    for f in peaks:
        t = float(tA[f])
        # confirmado por basic-pitch (empieza una nota) o un ataque armónico muy claro
        nb = fB[max(0, f - 4):f + 5].max()
        if nb < 0.12 and fA[f] < 0.45:
            continue
        cand.append(t)
    starts = sorted(notes, key=lambda n: n["start"])
    for n in starts:                         # ataques suaves que sólo ve basic-pitch
        if n["amp"] > 0.5 and not any(abs(n["start"] - t) < 0.08 for t in cand):
            cand.append(n["start"])
    cand.sort()
    # grupos de ataques muy seguidos: un rasgueo (cuerdas escalonadas, < 0,1 s) es un solo golpe;
    # 3 o más golpes que crecen durante más de 0,12 s son un rasgueado de dedos
    groups = []
    for t in cand:
        if groups and t - groups[-1][-1] < 0.075:
            groups[-1].append(t)
        else:
            groups.append([t])
    merged_t, is_rasg = [], []
    for gp in groups:
        if len(gp) >= 2 and 0.1 <= gp[-1] - gp[0] <= 0.4:
            merged_t.append(gp[0])
            is_rasg.append(True)
        else:
            merged_t.append(gp[0])
            is_rasg.append(False)
            # un rasgueo largo puede dejar un ataque suelto justo después: si es más flojo, sobra
    events = []
    for k, (t, rz) in enumerate(zip(merged_t, is_rasg)):
        f0 = int(t * SR / hop)
        # ¿apagado? cuánto baja el volumen 0,1-0,15 s después (sin llegar al golpe siguiente)
        nxt = merged_t[k + 1] if k + 1 < len(merged_t) else t + 1.0
        a_, b_ = t + 0.08, min(t + 0.15, nxt - 0.01)
        pk = float(rms_db[f0:f0 + 6].max()) if f0 < len(rms_db) else -99.0
        drop = (pk - float(rms_db[int(a_ * SR / hop):max(int(a_ * SR / hop) + 1, int(b_ * SR / hop))].mean())) \
            if b_ - a_ >= 0.025 else 0.0
        ps = sorted({n["pitch"] for n in starts if -0.05 <= n["start"] - t <= 0.08})   # notas nuevas
        jl, jh = band_jump(lowb, f0), band_jump(highb, f0)
        durs = [n["end"] - n["start"] for n in starts if -0.05 <= n["start"] - t <= 0.08]
        seg = rms_db[f0:f0 + int(0.3 * SR / hop)]
        peak = float(seg[:8].max()) if len(seg) else -99.0
        fall = np.nonzero(seg[int(np.argmax(seg[:8])) if len(seg) else 0:] < peak - 18)[0]
        decay = (fall[0] * hop / SR) if len(fall) else 0.3
        lin = 10 ** (peak / 20)
        jump = peak - float(rms_db[max(0, f0 - 3)])                # cuánto sube el volumen
        e = {"t": round(t, 4), "acc": float(lin), "jump": float(jump), "pitches": ps,
             "dur": float(np.median(durs)) if durs else decay, "decay": float(decay), "drop": float(drop),
             "jl": float(jl), "jh": float(jh)}
        if rz:
            e["kind"] = "R"
        if events and t - events[-1]["t"] < 0.13 and lin < 0.75 * events[-1]["acc"] and "kind" not in e \
                and jump < 6:
            continue                          # cola del rasgueo anterior, no un golpe nuevo
        events.append(e)
    if events:                                # acento = volumen del golpe (y cuánto sobresale)
        lv = np.array([e["acc"] for e in events])
        ref = np.percentile(lv, 95) + 1e-9
        for e in events:
            e["acc"] = float(np.clip(e["acc"] / ref, 0.1, 1.3) * (0.85 + 0.15 * np.clip(e["jump"] / 12, 0, 1)))

    merged = events

    beats = np.asarray(beats if beats is not None else [], float)
    for e in merged:
        if "kind" in e:
            continue
        ps = e["pitches"]
        on_beat = True
        if len(beats) > 1:
            k = int(np.clip(np.searchsorted(beats, e["t"]) - 1, 0, len(beats) - 2))
            ph = (e["t"] - beats[k]) / max(1e-6, beats[k + 1] - beats[k])
            on_beat = ph < 0.18 or ph > 0.85
        jl, jh = e["jl"], e["jh"]
        if jl >= 3 and jh < 1.5:                       # sólo suben los graves: pulgar
            e["kind"] = "P"
            e["pitches"] = [p for p in ps if p < 57][:1]
        elif jh >= 2 and jl < 3:                       # sólo suben las agudas: notas punteadas
            e["kind"] = "N"
            e["pitches"] = [p for p in ps if p >= 52][-3:]
        elif e["drop"] > 12:                           # se corta enseguida: apagado
            e["kind"] = "X"
        elif not on_beat and jl < 3 and e["acc"] < 0.75:
            e["kind"] = "U"                            # flojo, a contratiempo y sin bajo: hacia arriba
        else:
            e["kind"] = "D"
    # acentos relativos: 0.3 (flojo) … 1 (fuerte)
    if merged:
        a = np.array([e["acc"] for e in merged])
        lo, hi = np.percentile(a, 10), np.percentile(a, 95)
        for e in merged:
            e["acc"] = round(float(0.3 + 0.7 * np.clip((e["acc"] - lo) / max(1e-6, hi - lo), 0, 1)), 3)
    return merged
