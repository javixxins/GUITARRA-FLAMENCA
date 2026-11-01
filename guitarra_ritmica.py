"""Guitarra rítmica flamenca a partir de una línea de tiempo de acordes.

1. Posturas (voicings): diccionario de posturas flamencas típicas («por arriba» en Mi,
   «por medio» en La, cuerdas al aire) + generador que encuentra posturas tocables para
   cualquier acorde en cualquier tonalidad (raíz en el bajo, todas las notas del acorde,
   máximo 4 dedos o cejilla, abertura de 4 trastes).
2. Cejilla automática: como hace un guitarrista flamenco, busca el traste de cejilla con el
   que los acordes de la canción caen en las posturas abiertas más idiomáticas.
3. Conducción de voces: elige la secuencia de posturas que menos mueve la mano izquierda
   (programación dinámica sobre todas las candidatas), prefiriendo cuerdas al aire.
4. Patrones de rasgueo (rumba, tangos, sevillanas, bulerías, soleá, rasgueado de 4 dedos,
   arpegio…) colocados sobre el pulso real del original, con velocidad del rasgueo, acentos,
   swing y humanización. Cada cuerda suena como en una guitarra real: una cuerda sólo da una
   nota a la vez, el apagado corta lo que suena y al cambiar de acorde la mano levanta.
"""
import functools
import itertools

import numpy as np

STRINGS = (40, 45, 50, 55, 59, 64)       # Mi2 La2 Re3 Sol3 Si3 Mi4 (6ª … 1ª)
STRING_NAMES = ("6ª", "5ª", "4ª", "3ª", "2ª", "1ª")
QUAL = {
    "maj": (0, 4, 7), "min": (0, 3, 7), "7": (0, 4, 7, 10), "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10), "dim": (0, 3, 6), "sus4": (0, 5, 7),
}
ESSENTIAL = {"maj": (0, 4), "min": (0, 3), "7": (0, 4, 10), "maj7": (0, 4, 11), "min7": (0, 3, 10),
             "dim": (0, 3, 6), "sus4": (0, 5)}
PC = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7,
      "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}

# Posturas flamencas y abiertas de referencia (6ª -> 1ª, -1 = no suena), sin cejilla.
# (nombre, trastes, bonus de idiomaticidad: más negativo = más típica en flamenco)
FLAMENCO_SHAPES = {
    "E:maj":  [("Mi «por arriba»", (0, 2, 2, 1, 0, 0), -1.6)],
    "E:7":    [("Mi7 abierta", (0, 2, 0, 1, 0, 0), -1.2), ("Mi7 con 7ª arriba", (0, 2, 2, 1, 3, 0), -0.9)],
    "E:min":  [("Mi menor abierta", (0, 2, 2, 0, 0, 0), -1.2)],
    "F:maj":  [("Fa flamenco (cuerdas al aire)", (1, 3, 3, 2, 0, 0), -1.0), ("Fa con cejilla", (1, 3, 3, 2, 1, 1), -0.5)],
    "F:maj7": [("Fa maj7 flamenco", (1, 3, 3, 2, 0, 0), -1.2), ("Fa maj7", (-1, -1, 3, 2, 1, 0), -0.8)],
    "G:maj":  [("Sol abierta", (3, 2, 0, 0, 0, 3), -1.2), ("Sol flamenco", (3, 2, 0, 0, 3, 3), -1.0)],
    "G:7":    [("Sol7 abierta", (3, 2, 0, 0, 0, 1), -1.2)],
    "A:min":  [("La menor abierta", (-1, 0, 2, 2, 1, 0), -1.5)],
    "A:min7": [("La m7 abierta", (-1, 0, 2, 0, 1, 0), -1.2)],
    "A:maj":  [("La «por medio»", (-1, 0, 2, 2, 2, 0), -1.5)],
    "A:7":    [("La7 abierta", (-1, 0, 2, 0, 2, 0), -1.3)],
    "A#:maj": [("Sib «por medio» (cejilla 1)", (-1, 1, 3, 3, 3, 1), -0.8)],
    "A#:maj7": [("Sib maj7", (-1, 1, 3, 2, 3, 1), -0.6)],
    "C:maj":  [("Do abierta", (-1, 3, 2, 0, 1, 0), -1.4)],
    "C:7":    [("Do7 abierta", (-1, 3, 2, 3, 1, 0), -1.2)],
    "C:maj7": [("Do maj7", (-1, 3, 2, 0, 0, 0), -1.3)],
    "D:min":  [("Re menor abierta", (-1, -1, 0, 2, 3, 1), -1.4)],
    "D:min7": [("Re m7 abierta", (-1, -1, 0, 2, 1, 1), -1.2)],
    "D:maj":  [("Re abierta", (-1, -1, 0, 2, 3, 2), -1.3)],
    "D:7":    [("Re7 abierta", (-1, -1, 0, 2, 1, 2), -1.2)],
    "B:7":    [("Si7 abierta", (-1, 2, 1, 2, 0, 2), -1.3)],
    "B:min":  [("Si menor", (-1, 2, 4, 4, 3, 2), -0.6)],
    "E:sus4": [("Mi sus4", (0, 2, 2, 2, 0, 0), -1.1)],
    "A:sus4": [("La sus4", (-1, 0, 2, 2, 3, 0), -1.1)],
    "D:sus4": [("Re sus4", (-1, -1, 0, 2, 3, 3), -1.0)],
    "B:dim":  [("Si disminuido", (-1, 2, 3, 4, 3, -1), 0.0)],
    "G#:dim": [("Sol# disminuido", (4, 5, 6, 4, -1, -1), 0.0)],
}


def parse(label):
    if not label or label == "N":
        return None, None
    r, q = label.split(":")
    return PC[r], q


def notes_of(frets, capo=0):
    return [STRINGS[i] + f + capo for i, f in enumerate(frets) if f >= 0]


@functools.lru_cache(maxsize=512)
def generated_shapes(root, q, max_fret=12, top=10):
    """Posturas tocables de un acorde (sin cejilla): raíz en el bajo, notas del acorde."""
    pcs = {(root + i) % 12 for i in QUAL[q]}
    ess = {(root + i) % 12 for i in ESSENTIAL[q]}
    fifth = (root + 7) % 12
    options = []
    for s in range(6):
        fr = [f for f in range(0, max_fret + 1) if (STRINGS[s] + f) % 12 in pcs]
        options.append([-1] + fr)
    found = []
    for frets in itertools.product(*options):
        sounding = [i for i, f in enumerate(frets) if f >= 0]
        if len(sounding) < 4:
            continue
        k0 = sounding[0]
        if k0 > 2 or any(frets[i] < 0 for i in range(k0, 6)):   # sólo se apagan cuerdas graves
            continue
        if (STRINGS[k0] + frets[k0]) % 12 != root:
            continue
        pset = {(STRINGS[i] + frets[i]) % 12 for i in sounding}
        if not ess <= pset:
            continue
        fretted = [frets[i] for i in sounding if frets[i] > 0]
        if fretted and max(fretted) - min(fretted) > 3:
            continue
        fm = min(fretted) if fretted else 0
        barre = False
        if fretted:
            on_fm = [i for i in sounding if frets[i] == fm]
            if len(on_fm) >= 2 and all(frets[i] >= fm for i in range(on_fm[0], 6)):
                barre = True
        fingers = (1 + sum(1 for i in sounding if frets[i] > fm)) if barre else len(fretted)
        if fingers > 4:
            continue
        pos = float(np.mean(fretted)) if fretted else 0.0
        opens = sum(1 for i in sounding if frets[i] == 0)
        thirds = sum(1 for i in sounding if (STRINGS[i] + frets[i] - root) % 12 in (3, 4))
        cost = (0.15 * pos + 0.6 * (6 - len(sounding)) - 0.3 * opens + (0.8 if fifth not in pset and q != "dim" else 0)
                + 0.2 * max(0, thirds - 1) + (0.4 if barre else 0) + 0.06 * len(fretted))
        found.append((cost, frets, barre, fm))
    found.sort(key=lambda x: x[0])
    out = []
    for cost, frets, barre, fm in found[:top]:
        if barre:
            nm = f"cejilla en el traste {fm}"
        elif fm == 0 or min([f for f in frets if f > 0] or [0]) <= 3 and 0 in frets:
            nm = "posición abierta"
        else:
            nm = f"posición en el traste {fm}"
        out.append((nm, tuple(frets), cost))
    return out


def candidates(label, capo=0, flavor="flamenco"):
    """Posturas candidatas de un acorde con la cejilla dada. Devuelve [(nombre, trastes, coste)]
    con los trastes ya contados desde la cejilla (0 = cuerda al aire con cejilla)."""
    r, q = parse(label)
    if r is None:
        return []
    rel = (r - capo) % 12
    key = f"{['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][rel]}:{q}"
    out = []
    for nm, frets, bonus in FLAMENCO_SHAPES.get(key, []):
        if flavor != "flamenco" and "flamenco" in nm.lower() and "«" not in nm:
            bonus += 0.6   # el «color flamenco» sólo si se pide
        out.append((nm, frets, bonus))
    for nm, frets, cost in generated_shapes(rel, q, max_fret=12 - capo):
        if all(frets != f for _, f, _ in out):
            out.append((nm, frets, cost))
    return sorted(out, key=lambda x: x[2])[:8]


def choose_capo(labels_durs, mode="auto", flavor="flamenco"):
    """Traste de cejilla con el que la canción cae en las posturas más idiomáticas."""
    if mode not in ("auto", None):
        return int(mode)
    best = None
    for c in range(0, 8):
        cost = 0.0
        for lab, d in labels_durs:
            cand = candidates(lab, c, flavor)
            cost += (cand[0][2] if cand else 3.0) * d
        total_d = sum(d for _, d in labels_durs) or 1.0
        score = cost / total_d + 0.12 * c
        if best is None or score < best[0]:
            best = (score, c)
    return best[1]


def _distance(a, b):
    s, n = 0.0, 0
    for x, y in zip(a, b):
        if x > 0 and y > 0:
            s += abs(x - y)
            n += 1
    fa = [x for x in a if x > 0]
    fb = [x for x in b if x > 0]
    centre = abs((np.mean(fa) if fa else 0) - (np.mean(fb) if fb else 0))
    return (s / n if n else 0.0) * 0.5 + centre


def voice_lead(labels, capo, flavor="flamenco", weight=0.35):
    """Secuencia de posturas que menos mueve la mano (Viterbi sobre las candidatas)."""
    cands = [candidates(lab, capo, flavor) for lab in labels]
    idx = [i for i, c in enumerate(cands) if c]
    choice = [None] * len(labels)
    if not idx:
        return choice
    prev_cost = [c[2] for c in cands[idx[0]]]
    back = []
    for a, b in zip(idx, idx[1:]):
        cur, bp = [], []
        for nm, fr, cst in cands[b]:
            opts = [prev_cost[k] + weight * _distance(cands[a][k][1], fr) for k in range(len(cands[a]))]
            k = int(np.argmin(opts))
            cur.append(opts[k] + cst)
            bp.append(k)
        back.append(bp)
        prev_cost = cur
    k = int(np.argmin(prev_cost))
    for step in range(len(idx) - 1, -1, -1):
        i = idx[step]
        nm, fr, _ = cands[i][k]
        choice[i] = {"name": nm, "frets": list(fr), "capo": capo, "notes": notes_of(fr, capo)}
        if step > 0:
            k = back[step - 1][k]
    return choice


# ---------------------------------------------------------------- patrones de rasgueo
# Cada evento: (posición en pulsos desde el inicio del compás, golpe, acento 0-1)
#   D = rasgueo hacia abajo (de grave a agudo)   U = hacia arriba (sólo agudas)
#   R = rasgueado de 4 dedos (e-a-m-i)           X = apagado / golpe (chasquido)
#   T/P = pulgar en el bajo                      S1 S2 S3 = cuerda suelta (arpegio)
#   H = pinzamiento de las 3 agudas
PATTERNS = {
    "rumba": {"name": "Rumba flamenca (4/4)", "meter": 4, "events": [
        (0, "D", 1.0), (0.5, "U", 0.55), (1, "X", 0.9), (1.5, "U", 0.55),
        (2, "D", 0.85), (2.5, "U", 0.55), (3, "X", 0.9), (3.5, "U", 0.6)]},
    "tangos": {"name": "Tangos (4/4)", "meter": 4, "events": [
        (0, "T", 1.0), (0.5, "U", 0.5), (1, "D", 0.8), (1.5, "U", 0.5), (2, "R", 1.0), (3, "D", 0.85), (3.5, "U", 0.55)]},
    "sevillanas": {"name": "Sevillanas (3/4)", "meter": 3, "events": [
        (0, "R", 1.0), (1, "D", 0.7), (2, "D", 0.8), (2.5, "U", 0.55)]},
    "bulerias": {"name": "Bulerías (compás de 12, acentos en 3·6·8·10·12)", "meter": 12, "events": [
        (b, "R" if b in (2, 7) else "D", 1.0 if b in (2, 5, 7, 9, 11) else 0.45) for b in range(12)]},
    "solea": {"name": "Soleá (12 tiempos, lenta: arpegio y rasgueado en los acentos)", "meter": 12, "events": [
        ev for b in range(12) for ev in (
            [(b, "R", 1.0)] if b in (2, 5, 7, 9, 11) else [(b, "P", 0.6), (b + 0.5, "H", 0.45)])]},
    "rasgueado": {"name": "Rasgueado continuo de 4 dedos", "meter": 1, "events": [(0, "R", 0.9)]},
    "corcheas": {"name": "Corcheas abajo-arriba", "meter": 1, "events": [(0, "D", 0.9), (0.5, "U", 0.6)]},
    "negras": {"name": "Un rasgueo por pulso", "meter": 1, "events": [(0, "D", 0.9)]},
    "arpegio": {"name": "Arpegio (pulgar + i-m-a)", "meter": 1, "events": [
        (0, "P", 0.85), (0.25, "S3", 0.55), (0.5, "S2", 0.6), (0.75, "S1", 0.65)]},
    "cambios": {"name": "Un golpe en cada cambio de acorde (sostenido)", "meter": 1, "events": []},
}


def _beat_time(beats, b):
    """Tiempo (s) de una posición en pulsos (con decimales), siguiendo el pulso real."""
    n = len(beats)
    ibi = float(np.median(np.diff(beats))) if n > 1 else 0.5
    i = int(np.floor(b))
    f = b - i
    if i < 0:
        return beats[0] + b * ibi
    if i >= n - 1:
        return beats[-1] + (b - (n - 1)) * ibi
    return beats[i] + f * (beats[i + 1] - beats[i])


def strike(kind, t, voicing, acc, p, rng, beat_len):
    """Notas de un golpe: [(cuerda, tiempo, nota MIDI, velocidad, corta)]."""
    if not voicing:
        return []
    strings = [i for i, f in enumerate(voicing["frets"]) if f >= 0]
    midi = {i: STRINGS[i] + voicing["frets"][i] + voicing["capo"] for i in strings}
    base = p["vel"] * acc
    sp = p["speed"]
    out = []

    def sweep(order, t0, speed, vel, short=False):
        for k, s in enumerate(order):
            jitter = 1 + rng.normal(0, 0.15)
            v = vel * (0.9 + 0.2 * rng.random()) * (1 - 0.03 * k)
            out.append((s, t0 + k * speed * max(0.4, jitter), midi[s], v, short))

    if kind == "D":
        sweep(strings, t, sp, base)
    elif kind == "U":
        sweep(sorted(strings[-4:], reverse=True), t, sp * 0.8, base * 0.72)
    elif kind == "R":
        gap = min(0.055, beat_len / 6)
        for j, f in enumerate((0.62, 0.72, 0.84, 1.0)):
            sweep(strings, t + j * gap, sp * 0.55, base * f)
    elif kind == "X":
        sweep(strings, t, sp * 0.6, base * 0.75, short=True)
    elif kind in ("T", "P"):
        out.append((strings[0], t, midi[strings[0]], base, False))
    elif kind in ("S1", "S2", "S3"):
        s = 6 - int(kind[1])
        s = s if s in midi else strings[-1]
        out.append((s, t, midi[s], base, False))
    elif kind == "H":
        sweep([s for s in strings if s >= 3], t, 0.006, base)
    return out


def generate(segments, voicings, beats, downbeats, params, seed=7):
    """Notas de la guitarra rítmica. `segments` = [{start, end, label}], `voicings` alineadas."""
    pat = PATTERNS.get(params.get("pattern", "rumba"), PATTERNS["rumba"])
    rng = np.random.default_rng(seed)
    p = {
        "speed": float(params.get("strum_ms", 14)) / 1000.0,
        "vel": 55 + 70 * float(params.get("intensity", 0.7)),
        "accent": float(params.get("accent", 0.6)),
        "humanize": float(params.get("humanize", 0.4)),
        "swing": float(params.get("swing", 0.0)),
        "ring": float(params.get("ring", 0.8)),
    }
    beats = np.asarray(beats, float)
    if len(beats) < 2:
        return []
    beat_len = float(np.median(np.diff(beats)))
    seg_starts = np.array([s["start"] for s in segments])

    def seg_at(t):
        i = int(np.searchsorted(seg_starts, t + 1e-4, side="right") - 1)
        if 0 <= i < len(segments) and segments[i]["start"] - 1e-4 <= t < segments[i]["end"]:
            return i
        return None

    events = []                                   # (t, kind, acento, i_segmento)
    meter = pat["meter"]
    first_db = float(downbeats[0]) if len(downbeats) else float(beats[0])
    b0 = int(np.argmin(np.abs(beats - first_db)))
    b = b0 - meter * int(np.ceil(b0 / meter))      # primer compás que empieza en 0 o antes
    end_t = segments[-1]["end"] if segments else beats[-1]
    while _beat_time(beats, b) < end_t:
        for pos, kind, acc in pat["events"]:
            frac = pos - np.floor(pos)
            pos2 = pos + (p["swing"] * 0.33 if abs(frac - 0.5) < 1e-6 else 0)
            t = _beat_time(beats, b + pos2)
            if t < 0 or t >= end_t:
                continue
            a = 1 - p["accent"] + p["accent"] * acc
            events.append([t, kind, a])
        b += meter
    # golpe en cada cambio de acorde si no hay ya uno cerca (mantiene la armonía en su sitio)
    if params.get("hit_changes", True) or not pat["events"]:
        times = np.array([e[0] for e in events]) if events else np.zeros(0)
        for s in segments:
            if s.get("label") in (None, "N"):
                continue
            if not len(times) or np.min(np.abs(times - s["start"])) > 0.12 * beat_len + 0.03:
                events.append([s["start"], "D", 1.0])
    events.sort(key=lambda e: e[0])

    # humanizar
    hz = p["humanize"]
    for e in events:
        e[0] += rng.normal(0, 0.011 * hz)
        e[2] *= max(0.5, rng.normal(1, 0.08 * hz))

    hits = []                                     # (cuerda, t, nota, vel, corta, segmento)
    ahead = min(0.06, 0.15 * beat_len)           # si el acorde cambia justo después, ya se toca el nuevo
    for t, kind, acc in events:
        i = seg_at(max(0.0, t) + ahead)
        if i is None or segments[i].get("label") in (None, "N") or voicings[i] is None:
            continue
        for s, tt, n, v, short in strike(kind, max(0.0, t), voicings[i], acc, p, rng, beat_len):
            if tt >= segments[i]["end"] - 0.005:  # el rasgueo no sigue con el acorde viejo tras el cambio
                continue
            hits.append((s, tt, n, v, short, i, kind == "X"))
    hits.sort(key=lambda h: h[1])

    # duración: una cuerda suena hasta que se vuelve a pulsar, se apaga o cambia el acorde
    notes = []
    chokes = sorted({round(h[1], 3) for h in hits if h[6]})
    for k, (s, t, n, v, short, i, _) in enumerate(hits):
        nxt = next((h[1] for h in hits[k + 1:] if h[0] == s), None)
        end = segments[i]["end"] + 0.03
        if nxt is not None:
            end = min(end, nxt)
        ch = next((c for c in chokes if c > t + 0.02), None)
        if ch is not None:
            end = min(end, ch + 0.01)
        if short:
            end = min(end, t + 0.045)
        ring_max = 0.25 + 6.0 * p["ring"]
        end = min(end, t + ring_max)
        notes.append({"start": round(t, 4), "end": round(max(end, t + 0.04), 4), "pitch": int(n),
                      "vel": int(np.clip(round(v), 1, 127))})
    return notes
