"""Lee el instrumento Flamenco Dreams Guitar (DecentSampler) y carga sus muestras.

El plugin no es un VST compilado sino un preset de DecentSampler (.dspreset):
un XML que dice qué archivo WAV suena en cada nota. Aquí leemos ese XML y
cargamos los WAV originales para tocarlos nosotros mismos.
"""
import os
import threading
import re
from collections import OrderedDict

import numpy as np
import soundfile as sf

SR = 44100

# Sonidos que ofrece la app. "layers" = (archivo .dspreset, nombre del grupo, nivel)
SOUNDS = OrderedDict([
    ("estudio", {
        "name": "Guitarra de Estudio",
        "subtitle": "V2 · Recomendada: grabación limpia",
        "desc": "La guitarra Manuel Rodríguez grabada en estudio: limpia, nítida y con cuerpo. "
                "La más fiel para que suene como una grabación profesional.",
        "preset": "V2", "layers": [("StudioGuitar", 0.8)],
        "fx": {"reverb": 0.15, "room": 0.45, "damping": 0.2, "predelay": 0.012, "attack": 0.002, "release": 0.8,
               "filter": 22000, "volume": 0.9},
    }),
    ("escenario", {
        "name": "Guitarra de Escenario",
        "subtitle": "V2 · Manuel Rodríguez grabada en directo",
        "desc": "La guitarra Manuel Rodríguez grabada al aire libre, sobre el escenario. "
                "Sonido rústico y gitano. Es el sonido por defecto del plugin V2.",
        "preset": "V2", "layers": [("StageGuitar", 0.8)],
        "fx": {"reverb": 0.2, "room": 0.85, "damping": 0.2, "predelay": 0.012, "attack": 0.002, "release": 0.8,
               "filter": 22000, "volume": 0.9},
    }),
    ("mezcla", {
        "name": "Estudio + Escenario",
        "subtitle": "V2 · las dos guitarras a la vez",
        "desc": "Mezcla las dos grabaciones como los dos deslizadores del plugin V2. "
                "Puedes ajustar el nivel de cada una.",
        "preset": "V2", "layers": [("StudioGuitar", 0.6), ("StageGuitar", 0.8)],
        "fx": {"reverb": 0.25, "room": 0.85, "damping": 0.2, "predelay": 0.012, "attack": 0.002, "release": 0.8,
               "filter": 22000, "volume": 0.9},
    }),
    ("v1", {
        "name": "Flamenco Dreams V1",
        "subtitle": "V1 · clásica con reverb",
        "desc": "El preset original de la versión 1: guitarra de estudio con la reverb "
                "amplia del plugin V1 (45 %).",
        "preset": "V1", "layers": [("__main__", 1.0)],
        "fx": {"reverb": 0.45, "room": 0.7, "damping": 0.3, "predelay": 0.02, "attack": 0.002, "release": 1.0,
               "filter": 22000, "volume": 0.9},
    }),
])

PRESET_FILES = {"V1": "Flamenco Dreams Guitar V1.dspreset",
                "V2": "Flamenco Dreams Guitar V2.dspreset"}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _attrs(tag_text):
    return {k: v for k, v in _ATTR.findall(tag_text)}


def parse_dspreset(path):
    """Devuelve una lista de grupos: {attrs, samples:[{root, lo, hi, seq, path}]}."""
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    m = re.search(r"<groups[^>]*>(.*)</groups>", text, re.S)
    body = m.group(1) if m else text
    groups = []
    for gm in re.finditer(r"<group\b([^>]*)>(.*?)</group>", body, re.S):
        samples = []
        for sm in re.finditer(r"<sample\b([^>]*)/?>", gm.group(2)):
            a = _attrs(sm.group(1))
            if "path" not in a:
                continue
            samples.append({
                "root": int(a.get("rootNote", 60)),
                "lo": int(a.get("loNote", a.get("rootNote", 60))),
                "hi": int(a.get("hiNote", a.get("rootNote", 60))),
                "seq": int(a.get("seqPosition", 1)),
                "path": a["path"],
            })
        groups.append({"attrs": _attrs(gm.group(1)), "samples": samples})
    return groups


def _db(v):
    try:
        return 10 ** (float(str(v).lower().replace("db", "")) / 20)
    except ValueError:
        return 1.0


class Layer:
    """Un grupo de muestras con su mapa nota -> lista de archivos (round robin)."""

    def __init__(self, base_dir, samples, gain, pitched_roots=None, max_seconds=8.0):
        self.base_dir = base_dir
        self.gain = gain
        self.max_seconds = max_seconds
        by_root = {}
        for s in sorted(samples, key=lambda s: (s["root"], s["seq"])):
            lst = by_root.setdefault(s["root"], [])
            if s["path"] not in lst:
                lst.append(s["path"])
        # pitched_roots: para los pads, la nota real que suena en cada archivo
        self.files = []  # (root_real_en_semitonos, [paths])
        for root, paths in by_root.items():
            if pitched_roots is not None:
                for p in paths:
                    if p in pitched_roots:
                        self.files.append((pitched_roots[p], [p]))
            else:
                self.files.append((float(root), paths))
        self.files.sort(key=lambda x: x[0])
        self.roots = np.array([r for r, _ in self.files]) if self.files else np.array([60.0])
        self._rr = {}

    @property
    def range(self):
        return float(self.roots.min()), float(self.roots.max())

    def pick(self, pitch):
        """Elige la muestra más cercana a la nota y la siguiente del round robin."""
        i = int(np.argmin(np.abs(self.roots - pitch)))
        root, paths = self.files[i]
        k = self._rr.get(i, 0)
        self._rr[i] = k + 1
        return root, os.path.join(self.base_dir, paths[k % len(paths)])

    def reset(self):
        self._rr = {}


_sample_cache = OrderedDict()
_CACHE_MAX = 260
_cache_lock = threading.Lock()  # la preescucha y la conversión pueden cargar a la vez


def load_sample(path, max_seconds=8.0):
    """Carga un WAV como float32 estéreo a 44.1 kHz (con caché)."""
    key = (path, max_seconds)
    with _cache_lock:
        if key in _sample_cache:
            _sample_cache.move_to_end(key)
            return _sample_cache[key]
    info = sf.info(path)
    frames = min(info.frames, int(max_seconds * info.samplerate))
    data, sr = sf.read(path, frames=frames, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2]
    if sr != SR:
        n = int(round(len(data) * SR / sr))
        t = np.linspace(0, len(data) - 1, n)
        data = np.stack([np.interp(t, np.arange(len(data)), data[:, c]) for c in range(2)], 1).astype(np.float32)
    # quita el silencio inicial para que el ataque caiga en su sitio
    env = np.abs(data).max(axis=1)
    thr = max(env.max() * 0.02, 1e-4)
    start = int(np.argmax(env > thr))
    start = max(0, start - int(0.003 * SR))
    data = np.ascontiguousarray(data[start:])
    with _cache_lock:
        _sample_cache[key] = data
        while len(_sample_cache) > _CACHE_MAX:
            _sample_cache.popitem(last=False)
    return data


class Instrument:
    def __init__(self, plugin_dir):
        self.dir = plugin_dir
        self.groups = {}
        for key, fname in PRESET_FILES.items():
            p = os.path.join(plugin_dir, fname)
            if not os.path.exists(p):
                raise FileNotFoundError(f"No encuentro «{fname}» en {plugin_dir}")
            self.groups[key] = parse_dspreset(p)

    def _group(self, preset, name):
        groups = self.groups[preset]
        if name == "__main__":
            # el grupo grande de guitarra (notas 40-83)
            return max(groups, key=lambda g: len(g["samples"]))["samples"]
        for g in groups:
            if g["attrs"].get("name") == name or g["attrs"].get("tags") == name:
                return g["samples"]
        raise KeyError(name)

    def _group_gain(self, preset, name):
        if name.startswith("__"):
            return 1.0
        for g in self.groups[preset]:
            if g["attrs"].get("name") == name:
                return _db(g["attrs"].get("volume", "0dB"))
        return 1.0

    def layers(self, sound_id, levels=None):
        snd = SOUNDS[sound_id]
        out = []
        for i, (name, level) in enumerate(snd["layers"]):
            if levels and i < len(levels) and levels[i] is not None:
                level = float(levels[i])
            if level <= 0:
                continue
            samples = self._group(snd["preset"], name)
            gain = level * self._group_gain(snd["preset"], name)
            out.append(Layer(self.dir, samples, gain))
        return out

    def check(self):
        """Comprueba que existen todas las muestras que vamos a usar."""
        missing = []
        for sid, snd in SOUNDS.items():
            for name, _ in snd["layers"]:
                for s in self._group(snd["preset"], name):
                    if not os.path.exists(os.path.join(self.dir, s["path"])):
                        missing.append(s["path"])
        return sorted(set(missing))


def note_name(p):
    p = int(round(p))
    return f"{NOTE_NAMES[p % 12]}{p // 12 - 1}"
