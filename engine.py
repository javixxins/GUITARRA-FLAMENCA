"""Motor de conversión: WAV -> notas (MIDI) -> guitarra flamenca (WAV).

1. Transcribe el audio a notas con basic-pitch (Spotify), que es polifónico:
   detecta acordes, varias voces a la vez, dinámica y afinación.
2. Toca cada nota con las muestras reales de Flamenco Dreams Guitar,
   igual que lo haría DecentSampler (round robin, velocidad, ataque, release).
3. Aplica los efectos del plugin (filtro y reverb) y exporta WAV y MIDI.
"""
import logging
import math
import os
import threading
import warnings

import numpy as np
import soundfile as sf
from scipy import signal

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.ERROR)

import librosa  # noqa: E402
import pretty_midi  # noqa: E402
from basic_pitch import ICASSP_2022_MODEL_PATH  # noqa: E402
from basic_pitch import note_creation as infer  # noqa: E402
from basic_pitch.constants import AUDIO_N_SAMPLES, AUDIO_SAMPLE_RATE, FFT_HOP  # noqa: E402
from basic_pitch.inference import Model, unwrap_output, window_audio_file  # noqa: E402

from instrument import SOUNDS, SR, load_sample  # noqa: E402

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        _model = Model(ICASSP_2022_MODEL_PATH)
    return _model


def _predict_window(model, x):
    if model.model_type == Model.MODEL_TYPES.COREML:  # evita los print de depuración
        r = model.model.predict({"input_2": x})
        return {"note": r["Identity_1"], "onset": r["Identity_2"], "contour": r["Identity"]}
    return model.predict(x)


# ---------------------------------------------------------------- análisis

class Prepared:
    """Lo lento del análisis, hecho una sola vez por archivo: audio, modelo de notas y
    espectro. Con esto, cambiar sensibilidad, limpieza o modo es casi instantáneo."""

    def __init__(self, audio, duration, tempo, tuning, output, remove_percussion):
        self.audio, self.duration, self.tempo, self.tuning = audio, duration, tempo, tuning
        self.output, self.remove_percussion = output, remove_percussion
        self._rise = None

    @property
    def rise(self):
        if self._rise is None:
            self._rise = _harmonic_rise(self.audio)
        return self._rise


def analyze(path, opts, progress):
    """Devuelve (notas, info). Notas = lista de dict(start, end, pitch, vel, bend)."""
    prep = prepare(path, opts, progress)
    return notes_from(prep, opts, progress)


def prepare(path, opts, progress=lambda *a: None, p0=0.0, p1=1.0):
    """Parte lenta: lee el audio, separa la percusión y pasa el modelo de notas."""
    def prog(p, msg):  # reparte el progreso entre p0 y p1 (para la preescucha)
        progress(p0 * 100 + (p1 - p0) * p, msg)

    prog(2, "Leyendo el archivo de audio…")
    audio, _ = librosa.load(path, sr=AUDIO_SAMPLE_RATE, mono=True)
    if len(audio) < AUDIO_SAMPLE_RATE * 0.2:
        raise ValueError("El audio es demasiado corto.")
    duration = len(audio) / AUDIO_SAMPLE_RATE
    peak = float(np.abs(audio).max())
    if peak < 1e-4:
        raise ValueError("El archivo está en silencio.")
    audio = audio / peak * 0.9  # el modelo trabaja mejor con nivel constante

    remove_perc = bool(opts.get("remove_percussion", True))
    if remove_perc:
        prog(3, "Separando la percusión (baterías, palmas, golpes)…")
        margin = float(opts.get("hpss_margin", 1.0))
        audio = librosa.effects.harmonic(audio, margin=margin).astype(np.float32)
        audio = audio / (np.abs(audio).max() or 1) * 0.9

    prog(4, "Calculando tempo y afinación…")
    try:
        tempo = float(np.atleast_1d(librosa.beat.beat_track(y=audio, sr=AUDIO_SAMPLE_RATE)[0])[0])
        if not (40 <= tempo <= 240):
            tempo = 120.0
    except Exception:
        tempo = 120.0
    try:
        tuning = float(librosa.estimate_tuning(y=audio, sr=AUDIO_SAMPLE_RATE))  # en fracción de semitono
    except Exception:
        tuning = 0.0

    # ---- inferencia por ventanas (así podemos mostrar el progreso)
    model = get_model()
    n_overlap = 30
    overlap_len = n_overlap * FFT_HOP
    hop = AUDIO_N_SAMPLES - overlap_len
    padded = np.concatenate([np.zeros(overlap_len // 2, np.float32), audio.astype(np.float32)])
    n_win = max(1, math.ceil(len(padded) / hop))
    out = {"note": [], "onset": [], "contour": []}
    for i, (win, _) in enumerate(window_audio_file(padded, hop)):
        with _model_lock:
            r = _predict_window(model, np.expand_dims(win, axis=0).astype(np.float32))
        for k in out:
            out[k].append(r[k])
        prog(6 + 44 * (i + 1) / n_win, f"Escuchando las notas… ({i + 1}/{n_win})")
    output = {k: unwrap_output(np.concatenate(out[k]), len(audio), n_overlap) for k in out}
    global LAST_OUTPUT
    LAST_OUTPUT = output
    return Prepared(audio, duration, tempo, tuning, output, remove_perc)


def notes_from(prep, opts, progress=lambda *a: None):
    """Parte rápida: del resultado del modelo a notas, con los ajustes de detección."""
    audio, output, tempo, tuning, duration = prep.audio, prep.output, prep.tempo, prep.tuning, prep.duration
    progress(51, "Convirtiendo a notas MIDI…")
    sens = float(opts.get("sensitivity", 0.5))  # 0 = pocas notas, 1 = muchas
    onset_t = 0.75 - 0.5 * sens
    frame_t = 0.45 - 0.3 * sens
    min_len_ms = float(opts.get("min_note_ms", 70))
    min_freq = opts.get("min_freq") or None
    _, events = infer.model_output_to_notes(
        output, onset_thresh=onset_t, frame_thresh=frame_t,
        min_note_len=max(1, int(round(min_len_ms / 1000 * (AUDIO_SAMPLE_RATE / FFT_HOP)))),
        min_freq=float(min_freq) if min_freq else None, max_freq=None,
        multiple_pitch_bends=False, melodia_trick=True, midi_tempo=tempo)

    notes = []
    for start, end, pitch, amp, bends in events:
        bend = 0.0
        if bends:
            bend = float(np.median(bends)) / 3.0  # 3 bins por semitono
            bend = float(np.clip(bend, -0.5, 0.5))
        notes.append({"start": float(start), "end": float(max(end, start + 0.03)),
                      "pitch": int(pitch), "amp": float(amp), "bend": bend})
    notes.sort(key=lambda n: (n["start"], n["pitch"]))

    if opts.get("clean", True) and notes:
        progress(53, "Limpiando notas falsas (re-ataques y armónicos)…")
        notes = _cleanup(notes, output, audio, prep.rise, opts)

    if opts.get("mode") == "melodia":
        notes = _monophonic(notes)

    # velocidad: misma dinámica relativa que el original
    if notes:
        amps = np.array([n["amp"] for n in notes])
        lo, hi = np.percentile(amps, 5), np.percentile(amps, 98)
        span = max(hi - lo, 1e-3)
        for n in notes:
            v = 40 + 85 * np.clip((n["amp"] - lo) / span, 0, 1)
            n["vel"] = int(np.clip(round(v), 1, 127))

    info = {"duration": duration, "tempo": round(tempo, 1), "tuning_cents": round(tuning * 100, 1),
            "n_notes": len(notes)}
    if notes:
        ps = [n["pitch"] for n in notes]
        info.update(lowest=min(ps), highest=max(ps), polyphony=_max_polyphony(notes))
    return notes, info


_CQT_BPO = 36


def _harmonic_rise(audio):
    """Devuelve rise(p, t): cuánto sube (dB) la energía de los armónicos de la nota p en el instante t."""
    C = np.abs(librosa.cqt(audio, sr=AUDIO_SAMPLE_RATE, hop_length=FFT_HOP, fmin=librosa.midi_to_hz(21),
                           n_bins=_CQT_BPO * 8, bins_per_octave=_CQT_BPO))
    D = librosa.amplitude_to_db(C, ref=np.max)
    sec = AUDIO_SAMPLE_RATE / FFT_HOP

    def sustain(p, t0, t1):
        """dB que cae la nota entre su principio y su final (poco = nota sostenida)."""
        b = int(round((p - 21) * 3))
        if b + 1 >= D.shape[0]:
            return 99.0
        band = D[max(0, b - 1):b + 2].max(0)
        f0, f1 = int(t0 * sec), int(t1 * sec)
        if f1 - f0 < 8:
            return 99.0
        q = max(2, (f1 - f0) // 4)
        return float(band[f0 + 2:f0 + 2 + q].mean() - band[f1 - q:f1].mean())

    def rise(p, t):
        f = int(t * sec)
        rs = []
        for h in (1, 2, 3):
            b = int(round((p - 21 + 12 * np.log2(h)) * 3))
            if b + 1 >= D.shape[0]:
                break
            band = D[max(0, b - 1):b + 2].max(0)
            pre = band[max(0, f - 8):max(1, f - 2)].mean()
            post = band[f + 2:f + 8].mean() if f + 3 < len(band) else pre
            rs.append(post - pre)
        return float(np.median(rs)) if rs else 0.0

    rise.sustain = sustain
    return rise


GHOST_INTERVALS = (12, 19, 24, 28, 31, 36)


def _cleanup(notes, output, audio, rise, opts):
    """Quita las notas que no estaban en el original:
    1. re-ataques falsos: una nota que sigue sonando y se «vuelve a pulsar» sin que
       su energía suba (lo provoca el ataque de otra nota o un vibrato);
    2. armónicos fantasma: notas débiles una octava, quinta… por encima (o una
       octava por debajo) de otra nota más fuerte que empieza a la vez;
    3. notas sueltas muy cortas y débiles.
    """
    times = infer.model_frames_to_time(output["note"].shape[0])
    act = output["note"]
    rise_min = float(opts.get("retrigger_db", 1.5))

    # 1) re-ataques falsos -> se funden con la nota anterior de la misma altura
    by_pitch = {}
    for n in sorted(notes, key=lambda n: n["start"]):
        by_pitch.setdefault(n["pitch"], []).append(n)
    keep = []
    for p, lst in by_pitch.items():
        cur = None
        for n in lst:
            if cur is not None and n["start"] - cur["end"] < 0.08:
                f = int(np.searchsorted(times, n["start"]))
                was_on = act[max(0, f - 6):max(1, f - 2), p - 21].mean() > 0.3
                if was_on and rise(p, n["start"]) < rise_min:
                    cur["end"] = max(cur["end"], n["end"])
                    cur["amp"] = max(cur["amp"], n["amp"])
                    continue
            if cur is not None:
                keep.append(cur)
            cur = dict(n)
        if cur is not None:
            keep.append(cur)
    notes = sorted(keep, key=lambda n: (n["start"], n["pitch"]))

    # 2) armónicos fantasma
    ratio = float(opts.get("ghost_ratio", 0.6))
    drop = set()
    for i, n in enumerate(notes):
        for j, q in enumerate(notes):
            if i == j or j in drop:
                continue
            d = n["pitch"] - q["pitch"]
            if d not in GHOST_INTERVALS and d != -12:
                continue
            together = abs(n["start"] - q["start"]) < 0.07 or (q["start"] < n["start"] < q["end"] - 0.05)
            if together and n["amp"] < ratio * q["amp"] and rise(n["pitch"], n["start"]) < rise(q["pitch"], q["start"]) + 3:
                drop.add(i)
                break
    notes = [n for i, n in enumerate(notes) if i not in drop]

    # 2b) armónicos agudos: notas muy agudas más débiles que una nota grave que suena a la vez
    drop = set()
    for i, n in enumerate(notes):
        if n["pitch"] < 84:
            continue
        for q in notes:
            if q["pitch"] <= n["pitch"] - 12 and q["start"] - 0.07 < n["start"] < q["end"] and n["amp"] < 0.85 * q["amp"]:
                drop.add(i)
                break
    notes = [n for i, n in enumerate(notes) if i not in drop]

    # 3) notas sueltas muy cortas y débiles
    notes = [n for n in notes if not (n["end"] - n["start"] < 0.12 and n["amp"] < 0.4)]

    # 4) artefactos: notas en silencio y «racimos» imposibles (>10 notas a la vez)
    sr = AUDIO_SAMPLE_RATE
    peak = float(np.abs(audio).max()) or 1.0
    def loud(n):
        s = int(n["start"] * sr)
        seg = audio[s:s + int(0.1 * sr)]
        return len(seg) > 0 and float(np.sqrt(np.mean(seg ** 2))) > peak * 0.003
    notes = [n for n in notes if loud(n)]
    starts = np.array([n["start"] for n in notes])
    notes = [n for n in notes if np.sum(np.abs(starts - n["start"]) < 0.04) <= 10]

    # 5) ¿la nota se sostiene en el original (voz, violín, órgano) o se apaga (piano, guitarra)?
    for n in notes:
        d = n["end"] - n["start"]
        n["sustained"] = bool(d > 0.7 and rise.sustain(n["pitch"], n["start"], n["end"]) / d < 6.0)  # < 6 dB/s
    return notes


def _monophonic(notes):
    """Deja una sola voz: en cada momento, la nota más fuerte."""
    keep = []
    for n in sorted(notes, key=lambda n: -n["amp"]):
        if all(n["end"] <= k["start"] + 0.03 or n["start"] >= k["end"] - 0.03 for k in keep):
            keep.append(n)
    keep.sort(key=lambda n: n["start"])
    for a, b in zip(keep, keep[1:]):  # legato: cada nota acaba donde empieza la siguiente
        if b["start"] - a["end"] < 0.08:
            a["end"] = max(a["start"] + 0.03, b["start"])
    return keep


def _max_polyphony(notes):
    ev = sorted([(n["start"], 1) for n in notes] + [(n["end"], -1) for n in notes])
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


# ---------------------------------------------------------------- síntesis

def _shift(data, ratio, n_out):
    """Cambia la altura de una muestra remuestreando (como un sampler)."""
    if abs(ratio - 1.0) < 1e-4:
        out = data[:n_out]
    else:
        pos = np.arange(n_out, dtype=np.float64) * ratio
        pos = pos[pos < len(data) - 1]
        i = pos.astype(np.int64)
        frac = (pos - i).astype(np.float32)[:, None]
        out = data[i] * (1 - frac) + data[i + 1] * frac
    return out


def render(notes, sound_id, instrument, opts, progress, dry=False):
    """Toca las notas con las muestras del plugin. Devuelve audio estéreo float32."""
    snd = SOUNDS[sound_id]
    fx = dict(snd["fx"])
    for k in fx:
        if opts.get(k) is not None:
            fx[k] = float(opts[k])
    layers = instrument.layers(sound_id, opts.get("levels"))
    if not layers:
        raise ValueError("Todas las capas de sonido están a cero.")
    tuning = float(opts.get("tuning_cents", 0)) / 100.0 if opts.get("follow_tuning", True) else 0.0
    follow_bends = bool(opts.get("follow_bends", True))
    transpose = int(opts.get("transpose", 0))
    notes = guitarize(notes, opts, transpose)  # registro real y rasgueo de acordes
    transpose = 0

    total = (max(n["end"] for n in notes) if notes else 1.0) + fx["release"] + 4.0
    buf = np.zeros((int(total * SR), 2), np.float32)
    attack = max(0.0005, fx["attack"])
    release = max(0.02, fx["release"])

    # para cortar una nota cuando se vuelve a tocar la misma (como una cuerda)
    next_same = {}
    last = {}
    for idx in sorted(range(len(notes)), key=lambda i: notes[i]["start"], reverse=True):
        p = notes[idx]["pitch"]
        next_same[idx] = last.get(p)
        last[p] = notes[idx]["start"]

    for layer in layers:
        layer.reset()
    nl = len(layers)
    sos_cache = {}
    for idx, n in enumerate(notes):
        target = n["pitch"] + transpose + tuning + (n.get("bend", 0) if follow_bends else 0)
        stop = n["end"] + release
        if next_same.get(idx) is not None:
            stop = min(stop, next_same[idx] + 0.04)
        length = max(0.05, stop - n["start"])
        n_out = int(length * SR)
        vel = n.get("vel", 100) / 127.0
        gain = vel ** 1.4
        for layer in layers:
            lo, hi = layer.range
            t = target
            while t < lo - 12:   # fuera del rango de la guitarra: sube/baja octavas
                t += 12
            while t > hi + 12:
                t -= 12
            root, path = layer.pick(t)
            data = load_sample(path, layer.max_seconds)
            ratio = 2 ** ((t - root) / 12.0)
            seg = _shift(data, ratio, n_out)
            m = len(seg)
            if m == 0:
                continue
            env = np.ones(m, np.float32)
            a = min(m, int(attack * SR))
            if a > 1:
                env[:a] = np.linspace(0, 1, a, dtype=np.float32)
            note_len = int((min(n["end"], stop) - n["start"]) * SR)
            if note_len < m:
                r = m - note_len
                env[note_len:] *= np.cos(np.linspace(0, np.pi / 2, r, dtype=np.float32)) ** 2
            fe = min(m, int(0.03 * SR))  # sin clics si la muestra se acaba antes
            env[-fe:] *= np.linspace(1, 0, fe, dtype=np.float32)
            seg = seg * env[:, None]
            # notas suaves = algo más oscuras, como al pulsar flojo
            cutoff = int(2500 + 17500 * vel ** 2)
            if cutoff < 19000:
                key = cutoff // 250
                if key not in sos_cache:
                    sos_cache[key] = signal.butter(2, key * 250, fs=SR, output="sos")
                seg = signal.sosfilt(sos_cache[key], seg, axis=0).astype(np.float32)
            s = int(n["start"] * SR)
            e = min(len(buf), s + m)
            buf[s:e] += seg[: e - s] * (gain * layer.gain)
        if idx % 20 == 0 or idx == len(notes) - 1:
            progress(55 + 33 * (idx + 1) / max(1, len(notes)),
                     f"Tocando la guitarra… nota {idx + 1} de {len(notes)}")

    # recorta la cola en silencio
    env = np.abs(buf).max(axis=1)
    nz = np.nonzero(env > 1e-5)[0]
    end = (nz[-1] + 1) if len(nz) else SR
    buf = buf[: end + int(0.05 * SR)]

    if not dry:
        ref = opts.get("ref_path")
        orig = opts.get("ref_audio")  # ya cargado (preescucha): 44,1 kHz mono, sin percusión
        if orig is None and ref and (float(opts.get("follow_dynamics", 0.7)) > 0 or float(opts.get("copy_timbre", 0.35)) > 0):
            progress(89, "Analizando la onda del original…")
            orig, _ = librosa.load(ref, sr=SR, mono=True)
            if opts.get("remove_percussion", True):  # timbre y dinámica sólo de la parte musical
                orig = librosa.effects.harmonic(orig, margin=1.0)
        if orig is not None and float(opts.get("follow_dynamics", 0.7)) > 0:
            progress(90, "Copiando la dinámica del original…")
            buf = follow_dynamics(buf, orig, float(opts.get("follow_dynamics", 0.7)))
        progress(91, "Ecualizando: timbre del original + nitidez flamenca…")
        buf = tone_eq(buf, orig, float(opts.get("copy_timbre", 0.35)), float(opts.get("clarity", 0.6)),
                      opts.get("_ref_ltas"))
        if float(opts.get("clarity", 0.6)) > 0:
            buf = transient_shaper(buf, 0.5 * float(opts.get("clarity", 0.6)))
        progress(92, "Aplicando filtro y reverb del plugin…")
        if fx["filter"] < 20000:
            sos = signal.butter(4, max(60.0, fx["filter"]), fs=SR, output="sos")
            buf = signal.sosfilt(sos, buf, axis=0).astype(np.float32)
        if fx["reverb"] > 0.001:
            buf = reverb(buf, fx["reverb"], fx["room"], fx["damping"])
        progress(94, "Masterizando (compresión suave y limitador)…")
        buf = master(buf, float(opts.get("mastering", 0.5)), float(fx["volume"]))
        return buf.astype(np.float32)
    progress(95, "Ajustando volumen…")
    peak = float(np.abs(buf).max()) or 1.0
    return (buf * (10 ** (-1 / 20) / peak)).astype(np.float32)


def load_reference(path, remove_percussion=True):
    """El original a 44,1 kHz mono (sin percusión si se pide), para copiar dinámica y timbre."""
    orig, _ = librosa.load(path, sr=SR, mono=True)
    if remove_percussion:
        orig = librosa.effects.harmonic(orig, margin=1.0)
    return orig.astype(np.float32)


def _level_db(x):
    e = _env_db(x, 2048, 441)
    return float(np.percentile(e, 95)) if len(e) else -99.0


def render_excerpt(notes, sound_id, instrument, opts, ref, start, length, preroll=2.0):
    """Preescucha: toca sólo un fragmento [start, start+length) con la misma cadena que la
    conversión final. Empieza a tocar un poco antes (preroll) para que las notas que ya
    sonaban entren igual, y ajusta el volumen al que tendrá ese fragmento en el archivo final."""
    total = (len(ref) / SR) if ref is not None else max((n["end"] for n in notes), default=0)
    start = max(0.0, min(start, max(0.0, total - length)))
    s0 = max(0.0, start - preroll)
    t1 = start + length
    sel = [dict(n, start=n["start"] - s0, end=min(n["end"], t1 + 1.0) - s0)
           for n in notes if s0 <= n["start"] < t1]
    n_out = int(length * SR)
    if not sel:
        return np.zeros((n_out, 2), np.float32), start, 0
    r = ref[int(s0 * SR):int((t1 + 1.0) * SR)] if ref is not None else None
    o = dict(opts, ref_path=None, ref_audio=r)
    audio = render(sel, sound_id, instrument, o, lambda *a: None)
    a = int(round((start - s0) * SR))
    audio = audio[a:a + n_out]
    if len(audio) < n_out:
        audio = np.concatenate([audio, np.zeros((n_out - len(audio), 2), np.float32)])
    # el fragmento sale masterizado a tope; lo bajamos a su nivel real dentro de la pieza
    if ref is not None and length < total - 0.5:
        lvl_all = opts.get("_ref_level") if opts.get("_ref_level") is not None else _level_db(ref)
        lvl_here = _level_db(ref[int(start * SR):int(t1 * SR)])
        audio = audio * min(1.0, 10 ** (max(-24.0, lvl_here - lvl_all) / 20))
    f = int(0.01 * SR)  # sin clics al repetir en bucle
    audio[:f] *= np.linspace(0, 1, f, dtype=np.float32)[:, None]
    audio[-f:] *= np.linspace(1, 0, f, dtype=np.float32)[:, None]
    return audio.astype(np.float32), start, len(sel)


# ---------------------------------------------------------------- toque de guitarrista

def guitarize(notes, opts, transpose=0):
    """Prepara las notas para que suenen tocadas en una guitarra real:
    - registro: lo que una guitarra no alcanza se pasa a la octava que sí alcanza;
    - rasgueo: en los acordes las cuerdas no suenan a la vez, se rasguean
      (de grave a agudo hacia abajo, al revés hacia arriba si van seguidos)."""
    out = [dict(n, pitch=n["pitch"] + transpose) for n in notes]
    if opts.get("guitar_register", True):
        for n in out:
            while n["pitch"] < 38:   # Re grave (afinación en Drop D)
                n["pitch"] += 12
            while n["pitch"] > 88:   # Mi agudo, traste 24
                n["pitch"] -= 12
        # si al plegar coincide con una nota que ya suena igual, sobra
        seen, uniq = set(), []
        for n in sorted(out, key=lambda n: (n["start"], -n.get("vel", 100))):
            key = (n["pitch"], round(n["start"] / 0.04))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(n)
        out = uniq
    tremolo = opts.get("tremolo", "auto")
    if tremolo != "off":
        out = _tremolo(out, tremolo, float(opts.get("tremolo_rate", 9)))
    strum = float(opts.get("strum", 0.35))
    if strum > 0:
        out.sort(key=lambda n: n["start"])
        groups, cur = [], []
        for n in out:
            if cur and n["start"] - cur[0]["start"] > 0.035:
                groups.append(cur)
                cur = []
            cur.append(n)
        if cur:
            groups.append(cur)
        step = 0.022 * strum          # hasta 22 ms entre cuerdas
        last_t, down = -9.0, True
        for g in groups:
            if len(g) < 3:
                continue
            t0 = min(n["start"] for n in g)
            down = True if t0 - last_t > 0.3 else not down
            last_t = t0
            order = sorted(g, key=lambda n: n["pitch"], reverse=not down)
            for k, n in enumerate(order):
                n["start"] = t0 + k * step
                n["end"] = max(n["end"], n["start"] + 0.05)
                if k == 0 and down:  # el bordón un poco más presente
                    n["vel"] = min(127, int(n.get("vel", 100) * 1.08))
    return sorted(out, key=lambda n: (n["start"], n["pitch"]))


def _tremolo(notes, mode, rate):
    """Trémolo flamenco: una nota larga que en el original se sostiene (voz, violín…)
    se mantiene viva repitiéndola rápido, como hace el guitarrista. Sólo en la voz
    más aguda (la melodía); las notas graves siguen sonando de forma natural."""
    out = []
    period = 1.0 / max(4.0, rate)
    pattern = (1.0, 0.8, 0.88, 0.84)
    for n in notes:
        long_ = n["end"] - n["start"] > max(0.45, 4 * period)
        want = n.get("sustained", False) if mode == "auto" else long_
        top = not any(q is not n and q["pitch"] > n["pitch"] and q["start"] < n["end"] - 0.1 and q["end"] > n["start"] + 0.1
                      for q in notes)
        if not (want and long_ and top):
            out.append(n)
            continue
        t, k = n["start"], 0
        while t < n["end"] - period * 0.6:
            v = int(np.clip(n.get("vel", 100) * pattern[k % 4], 1, 127))
            out.append(dict(n, start=t, end=min(n["end"], t + period * 1.4), vel=v, tremolo=True))
            t += period
            k += 1
        out[-1]["end"] = n["end"]
    return out


# ---------------------------------------------------------------- análisis del original y sonido

def _env_db(x, win, hop):
    """Envolvente RMS en dB (una muestra cada `hop`)."""
    x = np.asarray(x, np.float32)
    n = 1 + max(0, len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = x[np.minimum(idx, len(x) - 1)]
    return 10 * np.log10(np.mean(fr ** 2, axis=1) + 1e-12)


def follow_dynamics(buf, orig, amount):
    """Copia la dinámica del original: donde el original crece o se apaga, la guitarra también."""
    hop, win = 441, 2048  # 10 ms / 46 ms
    g = buf.mean(axis=1)
    n = min(len(g), len(orig))
    eo = _env_db(orig[:n], win, hop)
    eg = _env_db(g[:n], win, hop)
    eo -= np.percentile(eo, 95)
    eg -= np.percentile(eg, 95)
    diff = np.clip(eo - eg, -12, 6)
    diff[(eo < -45) & (eg < -45)] = 0      # silencio en los dos: no tocar
    k = 13                                  # suavizado ~130 ms
    diff = np.convolve(diff, np.ones(k) / k, mode="same") * amount
    t = np.arange(len(diff)) * hop + win // 2
    gain_db = np.interp(np.arange(len(buf)), t, diff, left=diff[0] if len(diff) else 0, right=0)
    return (buf * (10 ** (gain_db / 20))[:, None]).astype(np.float32)


def _ltas(x, nfft=4096):
    f, p = signal.welch(x, fs=SR, nperseg=nfft)
    return f, 10 * np.log10(p + 1e-14)


def _smooth_oct(f, db, frac=3):
    """Suaviza un espectro en bandas de 1/frac de octava."""
    out = np.empty_like(db)
    for i, fi in enumerate(f):
        lo, hi = fi * 2 ** (-0.5 / frac), fi * 2 ** (0.5 / frac)
        m = (f >= lo) & (f <= hi)
        out[i] = db[m].mean() if m.any() else db[i]
    return out


def clarity_curve(f, amount):
    """Curva de «guitarra flamenca nítida» en dB: graves limpios, sin barro, con presencia y aire."""
    def bell(fc, g, q):
        return g / (1 + ((np.log2(np.maximum(f, 1) / fc)) * 2 * q) ** 2)
    hp = -24 * np.clip(np.log2(75 / np.maximum(f, 1)), 0, None)       # paso alto ~75 Hz
    curve = hp + bell(250, -3.0, 0.9) + bell(800, -1.0, 1.2) + bell(3500, 3.0, 0.8) \
        + 2.5 / (1 + np.exp(-(np.log2(np.maximum(f, 1)) - np.log2(9000)) * 3))
    return np.where(f < 75, hp, curve * amount)


def tone_eq(buf, orig, timbre_amount, clarity, orig_ltas=None):
    """Ecualizador de fase lineal: copia parte del timbre (balance tonal) del original
    y aplica la curva de nitidez de guitarra flamenca."""
    f = np.linspace(0, SR / 2, 1025)
    total = clarity_curve(f, clarity)
    if orig is not None and timbre_amount > 0:
        fo, po = orig_ltas if orig_ltas is not None else _ltas(orig)  # timbre de toda la pieza
        fg, pg = _ltas(buf.mean(axis=1))
        d = _smooth_oct(fo, po - (po.max() - pg.max()) - pg, 3)
        d -= np.median(d[(fo > 200) & (fo < 5000)])     # sólo la forma, no el volumen
        d = np.clip(d, -9, 9)
        d[(fo < 80) | (fo > 12000)] = 0
        total = total + np.interp(f, fo, d) * timbre_amount
    if np.allclose(total, 0):
        return buf
    gain = 10 ** (total / 20)
    taps = signal.firwin2(2049, f, gain, fs=SR)
    out = np.stack([signal.oaconvolve(buf[:, c], taps)[1024:1024 + len(buf)] for c in range(2)], 1)
    return out.astype(np.float32)


def transient_shaper(buf, amount):
    """Realza el ataque de cada pulsación (más definición, sonido más «nítido»)."""
    if amount <= 0:
        return buf
    x = np.abs(buf).max(axis=1)
    fast = signal.lfilter([1 - 0.995], [1, -0.995], x)       # ~5 ms
    slow = signal.lfilter([1 - 0.9995], [1, -0.9995], x)     # ~45 ms
    g = np.clip((fast + 1e-6) / (slow + 1e-6), 1, 4) ** (0.6 * amount)
    g = np.minimum(g, 10 ** (4 / 20))
    return (buf * g[:, None]).astype(np.float32)


def master(buf, amount, volume=0.9):
    """Compresión suave + limitador a −1 dBFS: sonido de grabación terminada."""
    peak = float(np.abs(buf).max()) or 1.0
    x = buf / peak
    if amount > 0:
        lvl = np.abs(x).max(axis=1)
        env = signal.lfilter([1 - 0.9993], [1, -0.9993], lvl)       # ~30 ms
        env_db = 20 * np.log10(env + 1e-9)
        thr = np.percentile(env_db[env_db > -60], 90) - 4 * amount if np.any(env_db > -60) else -20
        ratio = 1 + 2.0 * amount
        over = np.maximum(0, env_db - thr)
        gr = -over * (1 - 1 / ratio)
        gr = signal.lfilter([1 - 0.9998], [1, -0.9998], gr)         # suelta en ~110 ms
        x = x * (10 ** (gr / 20))[:, None]
        x /= np.abs(x).max() or 1.0
        x *= 10 ** (2.5 * amount / 20)                               # gana volumen
    # limitador con anticipación de 5 ms: nada pasa de −1 dBFS
    ceil = 10 ** (-1 / 20) * min(1.0, volume / 0.9)
    need = np.minimum(1.0, ceil / (np.abs(x).max(axis=1) + 1e-9))
    w = int(0.005 * SR)
    from scipy.ndimage import minimum_filter1d, uniform_filter1d
    g = minimum_filter1d(need, size=2 * w + 1)
    g = uniform_filter1d(g, size=w)
    g = minimum_filter1d(g, size=w)
    x = x * g[:, None]
    return np.clip(x, -ceil, ceil).astype(np.float32)


def reverb(x, wet, room, damping):
    """Reverb de sala (respuesta al impulso sintética, convolución por FFT)."""
    rt60 = 0.6 + 3.2 * float(room)
    n = int(min(rt60, 4.5) * SR)
    rng = np.random.default_rng(7)
    t = np.arange(n) / SR
    decay = np.exp(-6.9 * t / rt60)
    ir = np.zeros((n, 2), np.float32)
    bright = rng.standard_normal((n, 2))
    dark = signal.sosfilt(signal.butter(2, 2500, fs=SR, output="sos"), rng.standard_normal((n, 2)), axis=0) * 2.2
    mix = np.exp(-t * (1 + 8 * float(damping)))[:, None]
    ir[:] = ((bright * mix + dark * (1 - mix)) * decay[:, None]).astype(np.float32)
    pre = int(0.012 * SR)
    ir = np.concatenate([np.zeros((pre, 2), np.float32), ir])
    ir /= np.sqrt((ir ** 2).sum(axis=0, keepdims=True)) + 1e-9
    tail = np.zeros((len(ir), 2), np.float32)
    xx = np.concatenate([x, tail])
    w = np.stack([signal.oaconvolve(xx[:, c], ir[:, c])[: len(xx)] for c in range(2)], 1)
    w *= np.abs(xx).max() / (np.abs(w).max() + 1e-9) * 0.7
    out = xx * (1 - 0.5 * wet) + w * wet
    nz = np.nonzero(np.abs(out).max(axis=1) > 1e-5)[0]
    return out[: (nz[-1] + 1) if len(nz) else len(out)].astype(np.float32)


def harmonic_similarity(ref_path, audio):
    """Parecido armónico (0-1) entre el original y el resultado: compara qué notas
    suenan en cada momento (cromagrama), sin importar el instrumento."""
    a, _ = librosa.load(ref_path, sr=22050, mono=True)
    b = librosa.resample(audio.mean(axis=1), orig_sr=SR, target_sr=22050)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ca = librosa.feature.chroma_stft(y=a, sr=22050, n_fft=4096, hop_length=1024)
    cb = librosa.feature.chroma_stft(y=b, sr=22050, n_fft=4096, hop_length=1024)
    ea = librosa.feature.rms(y=a, frame_length=4096, hop_length=1024)[0][: ca.shape[1]]
    m = ea > ea.max() * 0.05
    sim = (ca * cb).sum(0) / (np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0) + 1e-9)
    return float(sim[m].mean()) if m.any() else 0.0


# ---------------------------------------------------------------- exportar

def write_midi(notes, path, tempo, name, transpose=0):
    pm = pretty_midi.PrettyMIDI(initial_tempo=float(tempo))
    inst = pretty_midi.Instrument(program=24, name=name)  # 24 = guitarra de nailon
    for n in notes:
        p = int(np.clip(n["pitch"] + transpose, 0, 127))
        inst.notes.append(pretty_midi.Note(velocity=int(n.get("vel", 100)), pitch=p,
                                           start=n["start"], end=n["end"]))
    pm.instruments.append(inst)
    pm.write(path)


def write_wav(audio, path, bits=24):
    sf.write(path, audio, SR, subtype="PCM_24" if int(bits) == 24 else "PCM_16")


def demo_notes():
    """Cadencia andaluza (Lam - Sol - Fa - Mi) en arpegio, para escuchar cada sonido."""
    chords = [[45, 52, 57, 60, 64], [43, 50, 55, 59, 62], [41, 48, 53, 57, 60], [40, 47, 52, 56, 59, 64]]
    notes, t = [], 0.0
    for ci, ch in enumerate(chords):
        for i, p in enumerate(ch):
            notes.append({"start": t + i * 0.09, "end": t + 1.15, "pitch": p, "vel": 92 - i * 4})
        t += 1.2 if ci < 3 else 0
    notes += [{"start": t + 1.25 + i * 0.16, "end": t + 1.4 + i * 0.16, "pitch": p, "vel": 100}
              for i, p in enumerate([64, 65, 64, 62, 60, 59, 57, 56, 52])]
    for n in notes[-1:]:
        n["end"] += 1.2
    return notes
