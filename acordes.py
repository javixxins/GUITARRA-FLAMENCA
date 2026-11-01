"""Detección de acordes (audio -> línea de tiempo de acordes) pensada para guitarra flamenca.

Combina varias técnicas de referencia en análisis musical (MIR), todas con librerías que
funcionan igual en Windows y macOS:

1. Pulso (beat tracking, librosa): los acordes cambian con el pulso, así que el análisis se
   hace por pulsos y no por ventanas arbitrarias.
2. Dos fuentes de «qué notas suenan», combinadas:
   a) Espectro CQT afinado (Constant-Q, 3 bandas por semitono, corregido a la afinación del
      original), blanqueado y con los armónicos separados por NNLS (mínimos cuadrados no
      negativos con un diccionario de notas con sus armónicos), como el NNLS-Chroma/Chordino.
   b) Las notas que oye basic-pitch (red neuronal de Spotify para transcripción polifónica).
3. Croma de bajos y croma de agudos por separado (idea de Chordino): el bajo decide la
   fundamental del acorde, los agudos su tipo.
4. Plantillas de acordes en las 12 tonalidades: mayor, menor, 7, maj7, m7, disminuido, sus4.
5. Modelo oculto de Márkov (HMM) decodificado con Viterbi + probabilidades por acorde
   (forward-backward) para la fiabilidad y las alternativas.
6. Tonalidad y modo (mayor, menor y modo flamenco / frigio) y corrección armónica modal: una
   segunda pasada favorece los acordes propios de la tonalidad (p. ej. la cadencia andaluza
   La m - Sol - Fa - Mi), sin impedir acordes de fuera si se oyen claramente.
7. Compás (3 o 4 tiempos) y primer tiempo de cada compás, a partir de dónde cambian los acordes.
"""
import numpy as np
import librosa
from scipy.optimize import nnls

from basic_pitch import note_creation as infer

SR = 22050
TUNE = {"sub": 1, "snap": False, "p_stay": None, "beta": 12.0, "bass_w": 0.35, "src": 0.8, "key_prior": True}
NOTE_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
ES_SHARP = ["Do", "Do#", "Re", "Re#", "Mi", "Fa", "Fa#", "Sol", "Sol#", "La", "La#", "Si"]
ES_FLAT = ["Do", "Reb", "Re", "Mib", "Mi", "Fa", "Solb", "Sol", "Lab", "La", "Sib", "Si"]

# tipo -> (intervalos, pesos de la plantilla, penalización por complejidad)
QUALITIES = {
    "maj":  ((0, 4, 7), (1.0, 1.0, 0.9), 0.0),
    "min":  ((0, 3, 7), (1.0, 1.0, 0.9), 0.0),
    "7":    ((0, 4, 7, 10), (1.0, 1.0, 0.8, 0.9), 0.035),
    "maj7": ((0, 4, 7, 11), (1.0, 1.0, 0.8, 0.9), 0.045),
    "min7": ((0, 3, 7, 10), (1.0, 1.0, 0.8, 0.9), 0.035),
    "dim":  ((0, 3, 6), (1.0, 1.0, 1.0), 0.07),
    "sus4": ((0, 5, 7), (1.0, 1.0, 0.9), 0.08),
}
Q_NAMES = list(QUALITIES)
SUFFIX = {"maj": "", "min": "m", "7": "7", "maj7": "maj7", "min7": "m7", "dim": "dim", "sus4": "sus4"}
N_STATES = 12 * len(Q_NAMES) + 1   # + «sin acorde»
NC = N_STATES - 1


def state(root, q):
    return Q_NAMES.index(q) * 12 + root


def unstate(s):
    if s == NC:
        return None, None
    return s % 12, Q_NAMES[s // 12]


def label_of(s):
    r, q = unstate(s)
    return "N" if r is None else f"{NOTE_SHARP[r]}:{q}"


def chord_names(root, q, flats=False):
    """(nombre en inglés, nombre en español) del acorde."""
    if root is None:
        return "N.C.", "Sin acorde"
    en = (NOTE_FLAT if flats else NOTE_SHARP)[root] + SUFFIX[q]
    es = (ES_FLAT if flats else ES_SHARP)[root] + SUFFIX[q]
    return en, es


# ---------------------------------------------------------------- pulso

def beat_grid(path, duration, tempo_hint=None):
    """Pulsos del original (con percusión: ayuda a encontrar el pulso). Rellena el principio y
    el final para que todo el audio quede cubierto."""
    y, _ = librosa.load(path, sr=SR, mono=True)
    hop = 512
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop, aggregate=np.median)
    kw = {"start_bpm": float(tempo_hint)} if tempo_hint else {}
    tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=hop, units="time", **kw)
    tempo = float(np.atleast_1d(tempo)[0])
    beats = np.asarray(beats, float)
    if len(beats) < 4 or not (40 <= tempo <= 240):
        tempo = tempo if 40 <= tempo <= 240 else 100.0
        beats = np.arange(0, duration, 60.0 / tempo)
    ibi = float(np.median(np.diff(beats)))
    # el tempo detectado a veces sale a la mitad (p. ej. 48 en vez de 96): se dobla el pulso
    while 60.0 / ibi < 72 and ibi > 0.3:
        mids = (beats[:-1] + beats[1:]) / 2
        beats = np.sort(np.concatenate([beats, mids]))
        ibi /= 2
        tempo = 60.0 / ibi
    pre = np.arange(beats[0] - ibi, -1e-6, -ibi)[::-1]
    post = np.arange(beats[-1] + ibi, duration + ibi * 0.5, ibi)
    beats = np.concatenate([pre, beats, post])
    beats = beats[(beats >= -1e-6) & (beats < duration)]
    if len(beats) == 0 or beats[0] > 1e-3:
        beats = np.concatenate([[0.0], beats])
    strength = np.interp(beats, librosa.frames_to_time(np.arange(len(env)), sr=SR, hop_length=hop), env)
    return tempo, beats, strength


# ---------------------------------------------------------------- qué notas suenan

LO, HI = 24, 96            # Do1 … Do7 (MIDI)
N_P = HI - LO + 1


def _nnls_dict(n_harm=8, decay=0.7):
    D = np.zeros((N_P, N_P))
    for j in range(N_P):
        for k in range(1, n_harm + 1):
            i = j + int(round(12 * np.log2(k)))
            if i < N_P:
                D[i, j] += decay ** (k - 1)
    return D


_D = _nnls_dict()


def _segment_mean(feat, times, bounds):
    """Media de cada columna de `feat` (tiempo en `times`) dentro de cada segmento de pulsos,
    sin el primer 10 % del pulso (el golpe del ataque ensucia la armonía)."""
    out = np.zeros((feat.shape[0], len(bounds) - 1))
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        a2 = a + 0.1 * (b - a)
        m = (times >= a2) & (times < b)
        if not m.any():
            m = (times >= a) & (times < b)
        if not m.any():
            j = int(np.clip(np.searchsorted(times, (a + b) / 2), 0, len(times) - 1))
            out[:, i] = feat[:, j]
        else:
            out[:, i] = feat[:, m].mean(axis=1)
    return out


def salience(prep, bounds):
    """Fuerza de cada nota (Do1-Do7) en cada pulso, combinando CQT+NNLS y basic-pitch."""
    y = prep.audio
    hop = 512
    C = np.abs(librosa.cqt(y, sr=SR, hop_length=hop, fmin=librosa.midi_to_hz(LO), n_bins=N_P * 3,
                           bins_per_octave=36, tuning=float(prep.tuning)))
    # blanqueado espectral (quita el color del instrumento y deja los picos de las notas)
    k = 36
    pad = np.pad(C, ((k // 2, k // 2), (0, 0)), mode="reflect")
    cs = np.cumsum(pad, axis=0)
    mean = (cs[k:] - cs[:-k])[: C.shape[0]] / k
    cs2 = np.cumsum(pad ** 2, axis=0)
    var = np.maximum((cs2[k:] - cs2[:-k])[: C.shape[0]] / k - mean ** 2, 1e-12)
    W = np.maximum(0.0, (C - mean) / np.sqrt(var))
    S = W.reshape(N_P, 3, -1).max(axis=1)              # a semitonos
    t_cqt = librosa.frames_to_time(np.arange(S.shape[1]), sr=SR, hop_length=hop)
    Sb = _segment_mean(S, t_cqt, bounds)
    nn = np.stack([nnls(_D, Sb[:, i])[0] for i in range(Sb.shape[1])], axis=1)   # sin armónicos

    act = prep.output["note"].T                         # 88 x frames (MIDI 21-108)
    t_bp = infer.model_frames_to_time(act.shape[1])
    bp = _segment_mean(act, t_bp, bounds)[LO - 21:HI - 21 + 1]

    def norm(x):
        return x / (x.max(axis=0, keepdims=True) + 1e-9)
    src = TUNE["src"]
    if isinstance(src, float):   # peso de basic-pitch frente a CQT+NNLS
        sal = src * norm(bp) + (1 - src) * norm(nn)
    else:
        sal = {"both": 0.5 * norm(nn) + 0.5 * norm(bp), "nnls": norm(nn), "bp": norm(bp)}[src]

    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    t_rms = librosa.frames_to_time(np.arange(len(rms)), sr=SR, hop_length=hop)
    e = _segment_mean(rms[None, :], t_rms, bounds)[0]
    return sal, e / (e.max() + 1e-9)


def _ramp(p, a, b, c, d):
    return np.clip(np.minimum((p - a) / max(1e-9, b - a), (d - p) / max(1e-9, d - c)), 0, 1)


def chromas(sal):
    p = np.arange(LO, HI + 1)
    wt = _ramp(p, 40, 50, 84, 96)                       # agudos: Mi2 … Do7
    wb = _ramp(p, 23, 28, 47, 57)                       # bajos: Mi1 … La2
    treble = np.zeros((12, sal.shape[1]))
    bass = np.zeros((12, sal.shape[1]))
    for i, pp in enumerate(p):
        treble[pp % 12] += wt[i] * sal[i]
        bass[pp % 12] += wb[i] * sal[i] ** 2            # en el bajo manda la nota más fuerte
    return treble, bass


# ---------------------------------------------------------------- puntuación y HMM

def _templates():
    T = np.zeros((N_STATES - 1, 12))
    pen = np.zeros(N_STATES - 1)
    for qi, q in enumerate(Q_NAMES):
        ivs, ws, p = QUALITIES[q]
        for r in range(12):
            s = qi * 12 + r
            for iv, w in zip(ivs, ws):
                T[s, (r + iv) % 12] = w
            pen[s] = p
    T /= np.linalg.norm(T, axis=1, keepdims=True)
    return T, pen


_T, _PEN = _templates()


def emissions(treble, bass, energy, beta=18.0, bass_w=0.55):
    """Log-verosimilitud de cada acorde en cada pulso."""
    tn = treble / (np.linalg.norm(treble, axis=0, keepdims=True) + 1e-9)
    cos = _T @ tn                                          # acordes x pulsos
    bshare = bass / (bass.sum(axis=0, keepdims=True) + 1e-9)
    roots = np.arange(N_STATES - 1) % 12
    score = cos - _PEN[:, None] + bass_w * (bshare[roots] - 1.0 / 12)
    nscore = np.full((1, cos.shape[1]), 0.45)
    silent = energy < 0.04
    nscore[0, silent] = 2.0                                # silencio: sin acorde
    S = np.vstack([score, nscore]) * beta
    S -= S.max(axis=0, keepdims=True)
    return S - np.log(np.exp(S).sum(axis=0, keepdims=True))


def _transitions(prior, p_stay):
    n = N_STATES
    pr = np.asarray(prior, float) + 1e-12
    A = np.tile(pr, (n, 1))
    np.fill_diagonal(A, 0.0)
    A = A / A.sum(axis=1, keepdims=True) * (1 - p_stay)
    np.fill_diagonal(A, p_stay)
    return np.log(A)


def viterbi(logE, logA):
    n, T = logE.shape
    delta = logE[:, 0] - np.log(n)
    psi = np.zeros((n, T), int)
    for t in range(1, T):
        cand = delta[:, None] + logA
        psi[:, t] = cand.argmax(axis=0)
        delta = cand.max(axis=0) + logE[:, t]
    path = np.zeros(T, int)
    path[-1] = int(delta.argmax())
    for t in range(T - 1, 0, -1):
        path[t - 1] = psi[path[t], t]
    return path


def posteriors(logE, logA):
    n, T = logE.shape
    la = np.zeros((n, T))
    lb = np.zeros((n, T))
    la[:, 0] = logE[:, 0] - np.log(n)
    for t in range(1, T):
        m = la[:, t - 1][:, None] + logA
        mx = m.max(axis=0)
        la[:, t] = mx + np.log(np.exp(m - mx).sum(axis=0)) + logE[:, t]
    for t in range(T - 2, -1, -1):
        m = logA + (logE[:, t + 1] + lb[:, t + 1])[None, :]
        mx = m.max(axis=1)
        lb[:, t] = mx + np.log(np.exp(m - mx[:, None]).sum(axis=1))
    g = la + lb
    g -= g.max(axis=0, keepdims=True)
    g = np.exp(g)
    return g / g.sum(axis=0, keepdims=True)


# ---------------------------------------------------------------- tonalidad y modo

MAJOR_PROF = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROF = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

TOQUES = {4: "por arriba", 9: "por medio", 11: "por granaína", 6: "por taranta", 8: "por minera", 1: "por rondeña"}


def diatonic_chords(tonic, mode):
    """Acordes propios de la tonalidad: {(raíz, tipo): peso}."""
    t = tonic
    if mode == "mayor":
        base = [(0, "maj"), (2, "min"), (4, "min"), (5, "maj"), (7, "maj"), (9, "min"), (11, "dim"),
                (7, "7"), (0, "maj7"), (5, "maj7"), (2, "min7"), (4, "min7"), (9, "min7"), (7, "sus4"), (0, "sus4")]
        extra = [(2, "7"), (4, "7"), (9, "7"), (10, "maj"), (5, "min")]        # dominantes secundarias, préstamos
    elif mode == "menor":
        base = [(0, "min"), (2, "dim"), (3, "maj"), (5, "min"), (7, "min"), (7, "maj"), (7, "7"), (8, "maj"),
                (10, "maj"), (0, "min7"), (5, "min7"), (3, "maj7"), (8, "maj7"), (10, "7"), (7, "sus4")]
        extra = [(5, "maj"), (0, "7"), (2, "min7"), (11, "dim")]
    else:  # flamenco: modo de Mi (frigio con tercera mayor en la tónica)
        base = [(0, "maj"), (0, "7"), (1, "maj"), (1, "maj7"), (3, "maj"), (3, "7"), (5, "min"), (5, "min7"),
                (8, "maj"), (8, "maj7"), (10, "min"), (10, "min7"), (0, "min"), (3, "maj7"), (8, "7"), (0, "sus4")]
        extra = [(10, "7"), (10, "maj"), (5, "7"), (7, "dim"), (7, "maj"), (2, "dim")]
    out = {((t + r) % 12, q): 1.0 for r, q in base}
    for r, q in extra:
        out.setdefault(((t + r) % 12, q), 0.55)
    return out


def estimate_key(path_states, treble, dur):
    """Tonalidad: perfiles de Krumhansl (mayor/menor) + evidencia de modo flamenco en los acordes."""
    prof = (treble * dur[None, :]).sum(axis=1)
    prof = prof / (prof.sum() + 1e-9)
    cands = []
    for t in range(12):
        cands.append((np.corrcoef(prof, np.roll(MAJOR_PROF, t))[0, 1], t, "mayor"))
        cands.append((np.corrcoef(prof, np.roll(MINOR_PROF, t))[0, 1], t, "menor"))
    cands.sort(reverse=True)
    score, tonic, mode = cands[0]

    # modo flamenco: tónica mayor X, acorde mayor medio tono por encima (bII) que resuelve en X
    # (Fa -> Mi), y suele aparecer la cadencia andaluza (iv - bIII - bII - I).
    chords = [unstate(s) for s in path_states]
    share = {}
    for (r, q), d in zip(chords, dur):
        if r is not None:
            share[(r, q)] = share.get((r, q), 0) + d
    total = sum(share.values()) or 1.0
    best_fl = None
    for X in range(12):
        major_x = (share.get((X, "maj"), 0) + share.get((X, "7"), 0)) / total
        bII = (share.get(((X + 1) % 12, "maj"), 0) + share.get(((X + 1) % 12, "maj7"), 0)) / total
        res = 0
        prev = None
        for (r, q) in chords:
            if r is None:
                continue
            if prev is not None and prev[0] == (X + 1) % 12 and prev[1] in ("maj", "maj7") and r == X and q in ("maj", "7"):
                res += 1
            if (r, q) != prev:
                prev = (r, q)
        last = next(((r, q) for r, q in reversed(chords) if r is not None), (None, None))
        fl = major_x * 1.0 + bII * 1.2 + 0.08 * min(res, 4) + (0.15 if last[0] == X and last[1] in ("maj", "7") else 0)
        if major_x > 0.12 and bII > 0.05 and res >= 1 and (best_fl is None or fl > best_fl[0]):
            best_fl = (fl, X)
    if best_fl is not None and best_fl[0] > 0.45:
        tonic, mode = best_fl[1], "flamenco"
    return tonic, mode, float(score)


def key_name(tonic, mode, flats):
    es = (ES_FLAT if flats else ES_SHARP)[tonic]
    if mode == "flamenco":
        toque = TOQUES.get(tonic)
        return f"{es} flamenco (modo frigio{', ' + toque if toque else ''})"
    return f"{es} {mode}"


def uses_flats(tonic, mode):
    rel_major = tonic if mode == "mayor" else (tonic + 3) % 12 if mode == "menor" else (tonic + 8) % 12
    return rel_major in (5, 10, 3, 8, 1, 6)   # Fa, Sib, Mib, Lab, Reb, Solb


# ---------------------------------------------------------------- compás

def meter_and_downbeats(path_states, strength):
    """Compás (3 o 4) y fase: dónde caen los primeros tiempos (los acordes suelen cambiar ahí)."""
    changes = np.array([i for i in range(1, len(path_states)) if path_states[i] != path_states[i - 1]])
    s = (strength - strength.mean()) / (strength.std() + 1e-9)
    best = None
    for m in (4, 3):
        for ph in range(m):
            on = ((changes - ph) % m == 0).mean() if len(changes) else 0.0
            acc = s[ph::m].mean() if len(s[ph::m]) else 0.0
            sc = on + 0.15 * acc + (0.06 if m == 4 else 0.0)
            if best is None or sc > best[0]:
                best = (sc, m, ph)
    return best[1], best[2]


def _snap_changes(path, bounds, y, unit):
    hop = 256
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop)
    t_env = librosa.frames_to_time(np.arange(len(env)), sr=SR, hop_length=hop)
    peaks = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=10, post_avg=10, delta=0.05 * env.max(), wait=4)
    pt, pv = t_env[peaks], env[peaks]
    b = bounds.copy()
    for i in range(1, len(path)):
        if path[i] == path[i - 1]:
            continue
        t = bounds[i]
        m = np.abs(pt - t) <= 0.45 * unit
        if m.any():
            cand = pt[m]
            w = pv[m] * (1 - np.abs(cand - t) / (0.9 * unit))   # fuerte y cerca
            nt = float(cand[np.argmax(w)])
            if b[i - 1] < nt < bounds[i + 1]:
                b[i] = nt
    return b


# ---------------------------------------------------------------- todo junto

def detect(path, prep, progress=lambda *a: None):
    progress(5, "Buscando el pulso…")
    tempo, beats, strength = beat_grid(path, prep.duration, prep.tempo)
    ibi = float(np.median(np.diff(beats))) if len(beats) > 1 else 0.5
    # se analiza por medios pulsos (los cambios de acorde quedan mejor situados), salvo si el
    # pulso ya es muy rápido
    sub = (2 if ibi > 0.33 else 1) if TUNE["sub"] == "auto" else int(TUNE["sub"])
    if sub > 1:
        mids = (beats[:-1] + beats[1:]) / 2
        grid = np.sort(np.concatenate([beats, mids, [beats[-1] + ibi / 2]]))
        grid = grid[grid < prep.duration]
    else:
        grid = beats
    bounds = np.concatenate([grid, [prep.duration]])
    dur = np.diff(bounds)

    progress(25, "Escuchando la armonía (espectro afinado y notas)…")
    sal, energy = salience(prep, bounds)
    treble, bass = chromas(sal)
    logE = emissions(treble, bass, energy, TUNE["beta"], TUNE["bass_w"])

    progress(60, "Encadenando los acordes (modelo de Márkov)…")
    p_stay = TUNE["p_stay"] or (0.82 if sub == 1 else 0.9)
    prior1 = np.ones(N_STATES)
    for s in range(N_STATES - 1):
        prior1[s] = 1.0 if unstate(s)[1] in ("maj", "min") else 0.6
    path1 = viterbi(logE, _transitions(prior1, p_stay))

    progress(72, "Tonalidad y corrección modal flamenca…")
    tonic, mode, kscore = estimate_key(path1, treble, dur)
    dia = diatonic_chords(tonic, mode)
    prior2 = np.full(N_STATES, 0.2)
    for s in range(N_STATES - 1):
        r, q = unstate(s)
        prior2[s] = dia.get((r, q), 0.2 if q in ("maj", "min") else 0.1)
    prior2[NC] = 0.3
    logA = _transitions(prior2 if TUNE["key_prior"] else prior1, p_stay)
    path = viterbi(logE, logA)
    post = posteriors(logE, logA)

    progress(88, "Compás y primeros tiempos…")
    beat_states = path[::sub] if sub > 1 else path
    meter, phase = meter_and_downbeats(beat_states, strength[:len(beat_states)])
    downbeats = beats[phase::meter]

    # cada cambio de acorde se ajusta al ataque más cercano (los acordes cambian con un golpe)
    if TUNE["snap"]:
        bounds = _snap_changes(path, bounds, prep.audio, ibi / sub)

    flats = uses_flats(tonic, mode)
    segs = []
    i = 0
    while i < len(path):
        j = i
        while j + 1 < len(path) and path[j + 1] == path[i]:
            j += 1
        s = int(path[i])
        pm = post[:, i:j + 1].mean(axis=1)
        r, q = unstate(s)
        en, es = chord_names(r, q, flats)
        order = [int(k) for k in np.argsort(-pm) if int(k) != s and int(k) != NC][:3]
        alts = []
        for k in order:
            rr, qq = unstate(k)
            aen, aes = chord_names(rr, qq, flats)
            alts.append({"label": label_of(k), "root": rr, "quality": qq, "en": aen, "es": aes, "p": round(float(pm[k]), 3)})
        segs.append({"start": round(float(bounds[i]), 3), "end": round(float(bounds[j + 1]), 3),
                     "label": label_of(s), "root": r, "quality": q, "en": en, "es": es,
                     "conf": round(float(pm[s]), 3), "alts": alts})
        i = j + 1
    chord_dur = sum(s["end"] - s["start"] for s in segs if s["root"] is not None) or 1.0
    reliab = sum(s["conf"] * (s["end"] - s["start"]) for s in segs if s["root"] is not None) / chord_dur
    progress(100, "Acordes listos")
    return {
        "tempo": round(float(60.0 / ibi), 1), "beats": [round(float(b), 3) for b in beats],
        "downbeats": [round(float(b), 3) for b in downbeats], "meter": int(meter),
        "key": {"tonic": int(tonic), "mode": mode, "name": key_name(tonic, mode, flats), "flats": flats,
                "en": (NOTE_FLAT if flats else NOTE_SHARP)[tonic] + ("" if mode == "mayor" else "m" if mode == "menor" else " phrygian")},
        "segments": segs, "reliability": round(float(reliab), 3), "duration": round(float(prep.duration), 3),
        "tuning_cents": round(float(prep.tuning) * 100, 1),
    }


def to_lab(segments):
    """Formato .lab (estándar de MIREX / mir_eval): inicio fin acorde."""
    return "\n".join(f"{s['start']:.3f}\t{s['end']:.3f}\t{s['label']}" for s in segments) + "\n"
