"""Ajuste recomendado: analiza la onda del original y busca los ajustes del plugin con los
que la guitarra flamenca se parece más al original, sin perder calidad de guitarra.

1. Carácter del original (medido en la onda): percusión, notas sostenidas, acordes,
   rapidez, registro, ambiente (reverb) y margen dinámico -> punto de partida razonado.
2. Búsqueda: toca fragmentos reales con el plugin probando sonidos y ajustes, y mide
   cada prueba contra el original:
     - parecido armónico (qué notas suenan en cada momento, cromagrama)
     - parecido de dinámica (envolvente de volumen)
     - parecido de timbre (balance tonal)
   Se queda con la mejor combinación. Los límites (nitidez mínima, timbre copiado
   máximo) garantizan que siga sonando a guitarra flamenca limpia.
"""
import numpy as np
import librosa

import engine
from engine import SR, AUDIO_SAMPLE_RATE

SOUND_IDS = ("estudio", "escenario", "mezcla", "v1")
SOUND_NAMES = {"estudio": "Guitarra de Estudio", "escenario": "Guitarra de Escenario",
               "mezcla": "Estudio + Escenario", "v1": "Flamenco Dreams V1"}


# ---------------------------------------------------------------- medidas de la onda

def percussion_ratio(path):
    """Fracción de la energía que es percusión (golpes sin nota)."""
    y, _ = librosa.load(path, sr=AUDIO_SAMPLE_RATE, mono=True, duration=120)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    H, P = librosa.decompose.hpss(S, margin=1.0)
    h, p = float((H ** 2).sum()), float((P ** 2).sum())
    return p / (h + p + 1e-12)


def ambience(ref):
    """Cuánto ambiente/reverb tiene el original. La reverb no deja que el sonido se corte de
    golpe: medimos la caída más rápida del volumen (en 100 ms). Seco: cientos de dB/s;
    con mucha reverb: no pasa de ~60 dB/s. Devuelve (0 = seco … 1 = muy reverberante, dB/s)."""
    e = engine._env_db(ref, 2048, 441)  # una medida cada 10 ms
    if len(e) < 30:
        return 0.3, 0.0
    e = np.convolve(e, np.ones(3) / 3, mode="same")
    d = (e[:-10] - e[10:]) / 0.1
    act = e[:-10] > np.percentile(e, 95) - 25
    fastest = float(d[act].max()) if act.any() else 0.0
    wet = float(np.clip((250 - fastest) / 190, 0, 1))
    return wet, fastest


def dynamic_range(ref):
    e = engine._env_db(ref, 2048, 441)
    act = e[e > np.percentile(e, 95) - 45]
    return float(np.percentile(act, 95) - np.percentile(act, 10)) if len(act) else 0.0


def note_stats(notes):
    if not notes:
        return {}
    starts = np.array([n["start"] for n in notes])
    durs = np.array([n["end"] - n["start"] for n in notes])
    pitches = np.array([n["pitch"] for n in notes])
    chords = 0
    groups = 0
    for t in np.unique(np.round(starts / 0.04)):
        k = int(np.sum(np.round(starts / 0.04) == t))
        groups += 1
        chords += k >= 3
    t_grid = np.arange(0, starts.max() + 1, 0.05)
    poly = np.array([np.sum((starts <= t) & (starts + durs > t)) for t in t_grid])
    sounding = poly[poly > 0]
    return {
        "n": len(notes),
        "sustained": float(np.mean([n.get("sustained", False) for n in notes])),
        "chord_frac": chords / max(1, groups),
        "mono_frac": float(np.mean(sounding <= 1)) if len(sounding) else 1.0,
        "short_frac": float(np.mean(durs < 0.15)),
        "out_of_range": float(np.mean((pitches < 38) | (pitches > 88))),
        "rate": len(notes) / max(1.0, starts.max() - starts.min()),
    }


# ---------------------------------------------------------------- puntuación

def _chroma(x):
    y = librosa.resample(x, orig_sr=SR, target_sr=22050)
    c = librosa.feature.chroma_stft(y=y, sr=22050, n_fft=4096, hop_length=1024)
    e = librosa.feature.rms(y=y, frame_length=4096, hop_length=1024)[0][: c.shape[1]]
    return c, e


def score(orig, audio, orig_chroma=None):
    """Parecido (0-1) de la guitarra con el original en un fragmento."""
    g = audio.mean(axis=1)
    n = min(len(orig), len(g))
    o, g = orig[:n], g[:n]
    ca, ea = orig_chroma if orig_chroma is not None else _chroma(o)
    cb, _ = _chroma(g)
    m = min(ca.shape[1], cb.shape[1])
    ca, cb, ea = ca[:, :m], cb[:, :m], ea[:m]
    act = ea > ea.max() * 0.05
    sim = (ca * cb).sum(0) / (np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0) + 1e-9)
    harm = float(sim[act].mean()) if act.any() else 0.0
    eo, eg = engine._env_db(o, 2048, 441), engine._env_db(g, 2048, 441)
    mk = eo > eo.max() - 40
    dyn = float(np.corrcoef(eo[mk], eg[mk])[0, 1]) if mk.sum() > 10 and eg[mk].std() > 1e-6 else 0.0
    fo, lo = engine._ltas(o)
    _, lg = engine._ltas(g)
    band = (fo > 150) & (fo < 8000)
    so = engine._smooth_oct(fo, lo, 3)[band]
    sg = engine._smooth_oct(fo, lg, 3)[band]
    tim = float(np.mean(np.abs((so - so.mean()) - (sg - sg.mean()))))  # dB de diferencia
    total = harm + 0.35 * max(0.0, dyn) - 0.012 * tim
    return {"total": total, "harm": harm, "dyn": dyn, "timbre_db": tim}


def pick_windows(notes, duration, length=8.0, count=2):
    """Los fragmentos con más actividad musical (y separados entre sí)."""
    if duration <= length + 1:
        return [0.0]
    starts = np.array([n["start"] for n in notes]) if notes else np.zeros(0)
    cands = np.arange(0, duration - length, 1.0)
    dens = np.array([np.sum((starts >= c) & (starts < c + length)) for c in cands])
    chosen = []
    for i in np.argsort(-dens):
        c = float(cands[i])
        if all(abs(c - x) >= length for x in chosen):
            chosen.append(c)
        if len(chosen) == count:
            break
    return sorted(chosen) or [0.0]


# ---------------------------------------------------------------- recomendación

def recommend(path, prep, ref, ref_level, ref_ltas, instrument, base_opts, progress, lock):
    """Devuelve {options, reasons, before, after, tested}. `lock` protege el motor de sonido."""
    progress(2, "Analizando la onda del original…")
    perc = percussion_ratio(path)
    wet, fall = ambience(ref)
    drange = dynamic_range(ref)
    det = {"sensitivity": 0.5, "min_note_ms": 70, "mode": "poli", "clean": True}
    notes, info = engine.notes_from(prep, det)
    st = note_stats(notes)
    reasons = []
    o = dict(base_opts)

    # --- punto de partida razonado
    o["remove_percussion"] = bool(prep.remove_percussion)
    if perc > 0.12:
        reasons.append(f"Hay percusión en el original ({perc:.0%} de la energía): se separa antes de detectar notas.")
    else:
        reasons.append(f"Casi no hay percusión ({perc:.0%}).")
    o["clean"] = True
    o["follow_tuning"] = True
    if abs(info.get("tuning_cents", 0)) > 10:
        reasons.append(f"El original está afinado {info['tuning_cents']:+.0f} cents respecto a 440 Hz: la guitarra lo respeta.")
    o["guitar_register"] = True
    if st.get("out_of_range", 0) > 0.03:
        reasons.append(f"El {st['out_of_range']:.0%} de las notas cae fuera de una guitarra: se pasan a su octava real.")
    if st.get("mono_frac", 0) > 0.9:
        reasons.append("Es sobre todo una melodía (una nota cada vez).")
    if st.get("sustained", 0) > 0.15:
        o["tremolo"] = "auto"
        reasons.append(f"El {st['sustained']:.0%} de las notas se sostiene (voz, cuerda, órgano): trémolo flamenco en ellas.")
    else:
        o["tremolo"] = "off"
        reasons.append("Las notas se apagan solas (piano, guitarra, pulsado): sin trémolo, la cuerda resuena natural.")
    if st.get("chord_frac", 0) > 0.25:
        o["strum"] = 0.45
        reasons.append(f"Muchos acordes ({st['chord_frac']:.0%} de los ataques): rasgueo flamenco marcado.")
    else:
        o["strum"] = 0.25
    if st.get("short_frac", 0) > 0.35 or st.get("rate", 0) > 8:
        o["min_note_ms"] = 50
        reasons.append("Pasajes rápidos: se aceptan notas más cortas para no perder ninguna.")
    else:
        o["min_note_ms"] = 70
    if drange > 22:
        o["mastering"] = 0.35
        reasons.append(f"Mucho contraste de volumen ({drange:.0f} dB): masterización suave para conservarlo.")
    else:
        o["mastering"] = 0.55
    o["clarity"] = 0.7  # nitidez flamenca alta, sin llegar a sonar dura
    o["attack"] = 0.002
    o["filter"] = 22000

    # --- búsqueda tocando fragmentos reales
    wins = pick_windows(notes, prep.duration)
    length = min(8.0, prep.duration)
    origs = []
    for w in wins:
        seg = ref[int(w * SR):int((w + length) * SR)]
        origs.append((seg, _chroma(seg)))
    cache_notes = {}
    tested = []

    def notes_for(op):
        key = (op["sensitivity"], op["min_note_ms"], op.get("mode", "poli"))
        if key not in cache_notes:
            cache_notes[key] = engine.notes_from(prep, op)
        return cache_notes[key]

    def evaluate(op):
        ns, inf = notes_for(op)
        ro = dict(op, tuning_cents=inf.get("tuning_cents", 0), _ref_level=ref_level, _ref_ltas=ref_ltas)
        parts = []
        for w, (seg, ch) in zip(wins, origs):
            with lock:
                a, _, _ = engine.render_excerpt(ns, op["sound"], instrument, ro, ref, w, length)
            parts.append(score(seg, a, ch))
        r = {k: float(np.mean([p[k] for p in parts])) for k in parts[0]}
        tested.append((dict(op), r))
        return r

    steps = (len(SOUND_IDS) + 2 + 2 + 2) + 1
    done = [0]

    def run(op, label):
        done[0] += 1
        progress(8 + 88 * done[0] / steps, f"Probando {label}… ({done[0]}/{steps})")
        return evaluate(op)

    o.setdefault("sensitivity", 0.5)
    o.setdefault("copy_timbre", 0.35)
    o.setdefault("follow_dynamics", 0.7)
    default_opts = dict(base_opts, sound="estudio", sensitivity=0.5, min_note_ms=70, copy_timbre=0.35,
                        follow_dynamics=0.7, strum=0.35, tremolo="auto", clarity=0.6, mastering=0.5)
    before = run(default_opts, "los ajustes de fábrica")

    # 1) sonido de guitarra (con su reverb y sala de fábrica, ajustada al ambiente del original)
    best, best_r = None, None
    by_sound = {}
    for sid in SOUND_IDS:
        op = dict(o, sound=sid, **_fx_for(sid, wet, instrument))
        r = run(op, f"el sonido «{SOUND_NAMES[sid]}»")
        by_sound[sid] = r
        if best_r is None or r["total"] > best_r["total"]:
            best, best_r = op, r
    o = best

    # 2) sensibilidad de detección, 3) timbre copiado, 4) dinámica copiada
    # la sensibilidad sólo cambia con una mejora clara: el parecido armónico premia un poco
    # las notas de más, y preferimos no añadir notas falsas
    for key, vals, label, margin in (("sensitivity", (0.35, 0.65), "la sensibilidad", 0.012),
                                     ("copy_timbre", (0.2, 0.5), "el timbre copiado", 0.002),
                                     ("follow_dynamics", (0.5, 0.9), "la dinámica copiada", 0.002)):
        for v in vals:
            op = dict(o, **{key: v})
            r = run(op, f"{label} {v:.0%}")
            if r["total"] > best_r["total"] + margin:
                best, best_r = op, r
        o = best

    # razones del resultado de la búsqueda
    ranked = sorted(by_sound.items(), key=lambda kv: -kv[1]["total"])
    reasons.insert(0, f"Sonido: «{SOUND_NAMES[o['sound']]}», el que más se parece a tu original "
                      f"({ranked[0][1]['harm']:.0%} de parecido armónico; "
                      + ", ".join(f"{SOUND_NAMES[k]} {v['harm']:.0%}" for k, v in ranked[1:]) + ").")
    if wet > 0.55:
        reasons.append(f"El original tiene bastante ambiente (su sonido nunca se corta de golpe): más reverb y sala.")
    elif wet < 0.2:
        reasons.append("El original es seco y cercano: reverb corta para que la guitarra suene limpia y definida.")
    reasons.append(f"Sensibilidad {o['sensitivity']:.0%}, timbre copiado {o['copy_timbre']:.0%} y dinámica copiada "
                   f"{o['follow_dynamics']:.0%}: los valores que dieron más parecido en las pruebas.")
    reasons.append("Nitidez flamenca 70 %: graves limpios, presencia y ataque de una grabación de calidad.")
    progress(100, "Ajuste recomendado listo")
    clean = {k: v for k, v in o.items() if not k.startswith("_") and k not in ("ref_path", "ref_audio")}
    return {"options": clean, "reasons": reasons, "before": before, "after": best_r,
            "tested": len(tested), "windows": wins, "length": length}


def _fx_for(sound_id, wet, instrument_mod=None):
    """Reverb y sala del sonido, ajustadas al ambiente medido en el original."""
    from instrument import SOUNDS
    fx = dict(SOUNDS[sound_id]["fx"])
    base = fx["reverb"]
    fx["reverb"] = round(float(np.clip(base * (0.6 + 0.9 * wet), 0.05, 0.55)), 2)
    fx["room"] = round(float(np.clip(fx["room"] * (0.75 + 0.4 * wet), 0.2, 0.95)), 2)
    return fx
