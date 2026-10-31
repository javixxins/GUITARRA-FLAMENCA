"""Servidor local de «Guitarra Flamenca»: convierte WAV en guitarra flamenca.

Uso:  python server.py [--no-browser]   (Windows, macOS y Linux)
Usa el primer puerto libre entre 8850 y 8859 (no toca los de otras apps).
"""
import io
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import engine
import instrument
import recomendar

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(HERE, "trabajos")
CACHE_DIR = os.path.join(HERE, "cache")
CONFIG = os.path.join(HERE, "config.json")
PORTS = range(8850, 8860)
PRESET = "Flamenco Dreams Guitar V2.dspreset"
SYSTEM = platform.system()  # "Windows", "Darwin" (macOS) o "Linux"


def find_plugin():
    """Busca la carpeta del plugin: junto a la app, o en las carpetas habituales del usuario."""
    env = os.environ.get("FLAMENCO_PLUGIN_DIR")
    if env and os.path.isfile(os.path.join(env, PRESET)):
        return env
    home = os.path.expanduser("~")
    roots = [HERE, os.path.join(HERE, "plugin")] + [os.path.join(home, d) for d in (
        "Documents", "Documentos", "Downloads", "Descargas", "Music", "Música", "Desktop", "Escritorio")]
    if SYSTEM == "Windows":
        roots += [os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            if PRESET in files:
                return cur
            depth = cur.count(os.sep) - base_depth
            dirs[:] = [] if depth >= 4 else [d for d in dirs if not d.startswith(".")
                                              and d not in ("node_modules", ".venv", "trabajos", "cache")]
    return ""

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB

jobs = {}
work_lock = threading.Lock()
_inst = {"obj": None, "dir": None, "error": None}


# ------------------------------------------------------------------ config

def load_config():
    cfg = {"plugin_dir": "", "last_folder": os.path.expanduser("~")}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if not os.path.isfile(os.path.join(cfg["plugin_dir"] or "", PRESET)):
        found = find_plugin()
        if found:
            cfg["plugin_dir"] = found
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


def save_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


def get_instrument():
    d = load_config()["plugin_dir"]
    if not d:
        _inst.update(obj=None, dir=d, error="Descárgalo (gratis) y elige su carpeta «Flamenco Dreams Guitar V2».")
        return None
    if _inst["obj"] is None or _inst["dir"] != d:
        try:
            _inst["obj"] = instrument.Instrument(d)
            _inst["dir"] = d
            _inst["error"] = None
        except Exception as e:
            _inst["obj"] = None
            _inst["dir"] = d
            _inst["error"] = str(e)
    return _inst["obj"]


# ------------------------------------------------------------------ páginas

@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/fondo.jpg")
def background():
    p = os.path.join(load_config()["plugin_dir"], "Images", "bg_FlamencoDreams.jpg")
    if not os.path.exists(p):
        abort(404)
    return send_file(p)


@app.route("/api/config")
def api_config():
    inst = get_instrument()
    cfg = load_config()
    sounds = [{"id": k, "name": v["name"], "subtitle": v["subtitle"], "desc": v["desc"],
               "fx": v["fx"], "layers": [{"name": "Estudio" if n == "StudioGuitar" else
                                          "Escenario" if n == "StageGuitar" else "Guitarra",
                                          "level": lv} for n, lv in v["layers"]]}
              for k, v in instrument.SOUNDS.items()]
    return jsonify({"app": "guitarra-flamenca", "plugin_dir": cfg["plugin_dir"],
                    "plugin_ok": inst is not None, "plugin_error": _inst["error"],
                    "last_folder": cfg["last_folder"], "sounds": sounds})


@app.route("/api/plugin-dir", methods=["POST"])
def api_plugin_dir():
    path = (request.json or {}).get("path", "").strip()
    if not os.path.isdir(path):
        return jsonify({"error": "Esa carpeta no existe."}), 400
    if not os.path.isfile(os.path.join(path, PRESET)):
        sub = os.path.join(path, "Flamenco Dreams Guitar V2")
        if os.path.isfile(os.path.join(sub, PRESET)):
            path = sub
    save_config(plugin_dir=path)
    get_instrument()
    return api_config()


@app.route("/api/demo/<sound_id>")
def api_demo(sound_id):
    """Una frase de ejemplo tocada con cada sonido, para escucharlo antes de elegir."""
    if sound_id not in instrument.SOUNDS:
        abort(404)
    inst = get_instrument()
    if inst is None:
        abort(503)
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, f"demo_{sound_id}.wav")
    if not os.path.exists(p):
        with work_lock:
            audio = engine.render(engine.demo_notes(), sound_id, inst, {}, lambda *a: None)
            engine.write_wav(audio, p, bits=16)
    return send_file(p, mimetype="audio/wav")


# ------------------------------------------------------------------ conversión

def new_job(**kw):
    jid = uuid.uuid4().hex[:10]
    d = os.path.join(JOBS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    jobs[jid] = {"id": jid, "dir": d, "status": "queued", "progress": 0,
                 "message": "En cola…", "created": time.time(), **kw}
    return jobs[jid]


def run_job(job, opts, reuse_notes=None, prev=None):
    def progress(p, msg):
        job["progress"] = round(float(p), 1)
        job["message"] = msg

    try:
        inst = get_instrument()
        if inst is None:
            raise RuntimeError("No encuentro el plugin Flamenco Dreams Guitar. " + (_inst["error"] or ""))
        job["status"] = "running"
        with work_lock:
            rp = bool(opts.get("remove_percussion", True))
            prep = prev["preps"].get(rp) if prev else None
            if reuse_notes is None and prep is not None:
                progress(50, "Usando el análisis de la preescucha…")
                notes, info = engine.notes_from(prep, opts, progress)
            elif reuse_notes is None:
                notes, info = engine.analyze(job["input"], opts, progress)
            else:
                notes, info = reuse_notes
                progress(52, "Usando las notas ya detectadas…")
            job["notes"], job["info"] = notes, info
            if not notes:
                raise ValueError("No he encontrado notas en el audio. Prueba a subir la sensibilidad.")
            sid = opts.get("sound", "escenario")
            ropts = dict(opts, tuning_cents=info.get("tuning_cents", 0), ref_path=job["input"])
            if prep is not None and prev["refs"].get(rp) is not None:
                ropts["ref_audio"] = prev["refs"][rp]  # ya calculado: ahorra unos segundos
            audio = engine.render(notes, sid, inst, ropts, progress)

            progress(95, "Midiendo el parecido con el original…")
            try:
                info["similarity"] = round(100 * engine.harmonic_similarity(job["input"], audio))
            except Exception:
                pass
            progress(96, "Guardando WAV y MIDI…")
            base = job["basename"]
            wav = os.path.join(job["dir"], f"{base}.wav")
            mid = os.path.join(job["dir"], f"{base}.mid")
            engine.write_wav(audio, wav, bits=opts.get("bits", 24))
            transpose = int(opts.get("transpose", 0))
            engine.write_midi(notes, mid, info["tempo"],
                              "Flamenco Dreams Guitar - " + instrument.SOUNDS[sid]["name"], transpose)
            progress(98, "Preparando la escucha del MIDI…")
            preview = os.path.join(job["dir"], "midi_preview.wav")
            engine.write_wav(midi_preview(notes, transpose), preview, bits=16)
        job["files"] = {"wav": wav, "mid": mid, "midi_preview": preview}
        job["sound"] = sid
        job["opts"] = opts
        job["status"] = "done"
        progress(100, "¡Conversión terminada!")
    except Exception as e:
        traceback.print_exc()
        job["status"] = "error"
        job["message"] = str(e)


def midi_preview(notes, transpose=0):
    """El MIDI tal cual, tocado con un sonido neutro (para comprobar las notas)."""
    sr = instrument.SR
    end = max(n["end"] for n in notes) + 1.0
    out = np.zeros(int(end * sr), np.float32)
    for n in notes:
        f = 440.0 * 2 ** ((n["pitch"] + transpose - 69) / 12)
        d = n["end"] - n["start"] + 0.25
        t = np.arange(int(d * sr)) / sr
        y = sum(np.sin(2 * np.pi * f * h * t) / h ** 2 for h in (1, 2, 3, 4) if f * h < sr / 2)
        env = np.exp(-t * 3.0) * np.minimum(1, t / 0.005)
        rel = int(0.25 * sr)
        env[-rel:] *= np.linspace(1, 0, rel)
        s = int(n["start"] * sr)
        seg = (y * env * (n.get("vel", 100) / 127)).astype(np.float32)
        out[s:s + len(seg)] += seg[: len(out) - s]
    out /= max(1e-6, np.abs(out).max()) / 0.85
    return np.stack([out, out], 1)


def safe_name(name):
    name = os.path.splitext(os.path.basename(name or "audio"))[0]
    name = re.sub(r"[^\w\-. ()áéíóúñÁÉÍÓÚÑüÜ]", "_", name).strip() or "audio"
    return name[:80]


@app.route("/api/convert", methods=["POST"])
def api_convert():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No has subido ningún archivo."}), 400
    if not f.filename.lower().endswith((".wav", ".wave")):
        return jsonify({"error": "El archivo tiene que ser WAV."}), 400
    opts = json.loads(request.form.get("options", "{}"))
    sid = opts.get("sound", "escenario")
    if sid not in instrument.SOUNDS:
        return jsonify({"error": "Sonido desconocido."}), 400
    src = safe_name(f.filename)
    job = new_job(source=src, basename=f"{src} - guitarra flamenca ({instrument.SOUNDS[sid]['name']})")
    job["input"] = os.path.join(job["dir"], "original.wav")
    f.save(job["input"])
    prev = previews.get(request.form.get("preview_id", ""))
    threading.Thread(target=run_job, args=(job, opts, None, prev), daemon=True).start()
    return jsonify({"id": job["id"]})


# ------------------------------------------------------------------ preescucha en directo
#
# Al subir el archivo se hace una vez lo lento (separar la percusión y pasar el modelo de
# notas). Después, cada vez que se toca un control, se recalculan las notas y se toca sólo
# un fragmento (~10 s) con la misma cadena que la conversión final: tarda alrededor de 1 s.

previews = {}
preview_lock = threading.Lock()
PREVIEW_KEEP = 3


def _prepare_preview(sess, rp):
    def progress(p, msg):
        sess["progress"], sess["message"] = round(float(p), 1), msg
    try:
        sess["status"] = "preparing"
        prep = engine.prepare(sess["path"], {"remove_percussion": rp}, progress, 0.0, 0.85)
        progress(86, "Analizando la onda del original…")
        prep.rise  # el espectro para limpiar notas: mejor ahora que en el primer retoque
        ref = engine.load_reference(sess["path"], rp)
        sess["levels"][rp] = engine._level_db(ref)
        sess["ltas"][rp] = engine._ltas(ref)
        sess["refs"][rp] = ref
        sess["preps"][rp] = prep
        sess["duration"] = prep.duration
        sess["status"] = "ready"
        progress(100, "Preescucha lista")
    except Exception as e:
        traceback.print_exc()
        sess["status"], sess["message"] = "error", str(e)


@app.route("/api/preview/start", methods=["POST"])
def api_preview_start():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".wav", ".wave")):
        return jsonify({"error": "El archivo tiene que ser WAV."}), 400
    opts = json.loads(request.form.get("options", "{}"))
    for old in sorted(previews.values(), key=lambda x: x["created"])[:-(PREVIEW_KEEP - 1) or None]:
        previews.pop(old["id"], None)
        shutil.rmtree(old["dir"], ignore_errors=True)
    sid = uuid.uuid4().hex[:10]
    d = os.path.join(JOBS_DIR, "pre_" + sid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "original.wav")
    f.save(path)
    sess = {"id": sid, "dir": d, "path": path, "created": time.time(), "status": "queued", "progress": 0,
            "message": "Preparando…", "preps": {}, "refs": {}, "levels": {}, "ltas": {}, "serial": 0,
            "notes_key": None, "notes": None, "info": None, "duration": None}
    previews[sid] = sess
    rp = bool(opts.get("remove_percussion", True))
    threading.Thread(target=_prepare_preview, args=(sess, rp), daemon=True).start()
    return jsonify({"id": sid})


@app.route("/api/preview/<sid>")
def api_preview_status(sid):
    sess = previews.get(sid)
    if not sess:
        return jsonify({"error": "La preescucha ha caducado; vuelve a elegir el archivo."}), 404
    return jsonify({k: sess.get(k) for k in ("id", "status", "progress", "message", "duration")})


DETECTION_KEYS = ("sensitivity", "min_note_ms", "mode", "min_freq", "clean")


@app.route("/api/preview/<sid>/play", methods=["POST"])
def api_preview_play(sid):
    """Devuelve el fragmento convertido con los ajustes actuales (WAV 16 bits)."""
    sess = previews.get(sid)
    if not sess:
        return jsonify({"error": "La preescucha ha caducado; vuelve a elegir el archivo."}), 404
    data = request.json or {}
    serial = int(data.get("serial", 0))
    sess["serial"] = max(sess["serial"], serial)
    opts = data.get("options", {})
    rp = bool(opts.get("remove_percussion", True))
    if rp not in sess["preps"]:
        if sess["status"] != "preparing":  # han cambiado «quitar percusión»: hay que volver a escuchar
            threading.Thread(target=_prepare_preview, args=(sess, rp), daemon=True).start()
        return jsonify({"status": "preparing", "progress": sess["progress"], "message": sess["message"]}), 202
    inst = get_instrument()
    if inst is None:
        return jsonify({"error": "Falta el plugin de guitarra."}), 503
    with preview_lock:
        if serial < sess["serial"]:
            return ("", 204)  # ya hay una petición más nueva: ésta sobra
        t0 = time.time()
        key = json.dumps([rp] + [opts.get(k) for k in DETECTION_KEYS])
        if sess["notes_key"] != key:
            sess["notes"], sess["info"] = engine.notes_from(sess["preps"][rp], opts)
            sess["notes_key"] = key
        notes, info = sess["notes"], sess["info"]
        sid_sound = opts.get("sound", "estudio")
        if sid_sound not in instrument.SOUNDS:
            return jsonify({"error": "Sonido desconocido."}), 400
        length = float(np.clip(float(data.get("length", 10)), 3, 30))
        ropts = dict(opts, tuning_cents=info.get("tuning_cents", 0),
                     _ref_level=sess["levels"][rp], _ref_ltas=sess["ltas"][rp])
        try:
            audio, start, n = engine.render_excerpt(notes, sid_sound, inst, ropts, sess["refs"][rp],
                                                    float(data.get("start", 0)), length)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"No he podido preparar la preescucha: {e}"}), 500
        buf = io.BytesIO()
        sf.write(buf, audio, instrument.SR, format="WAV", subtype="PCM_16")
        buf.seek(0)
        resp = send_file(buf, mimetype="audio/wav")
        resp.headers["X-Start"] = f"{start:.3f}"
        resp.headers["X-Notes"] = str(n)
        resp.headers["X-Total-Notes"] = str(len(notes))
        resp.headers["X-Ms"] = str(int((time.time() - t0) * 1000))
        resp.headers["Cache-Control"] = "no-store"
        return resp


@app.route("/api/preview/<sid>/recommend", methods=["POST", "GET"])
def api_preview_recommend(sid):
    """Ajuste recomendado: analiza la onda del original y prueba ajustes del plugin."""
    sess = previews.get(sid)
    if not sess:
        return jsonify({"error": "La preescucha ha caducado; vuelve a elegir el archivo."}), 404
    if request.method == "GET":
        r = sess.get("rec") or {"status": "none"}
        return jsonify({k: v for k, v in r.items()})
    opts = (request.json or {}).get("options", {})
    rp = bool(opts.get("remove_percussion", True))
    if rp not in sess["preps"]:
        return jsonify({"error": "Espera a que termine de prepararse la preescucha."}), 409
    if (sess.get("rec") or {}).get("status") == "running":
        return jsonify({"ok": True})
    inst = get_instrument()
    if inst is None:
        return jsonify({"error": "Falta el plugin de guitarra."}), 503
    rec = {"status": "running", "progress": 0, "message": "Empezando…"}
    sess["rec"] = rec

    def progress(p, msg):
        rec["progress"], rec["message"] = round(float(p), 1), msg

    def work():
        try:
            rec["result"] = recomendar.recommend(sess["path"], sess["preps"][rp], sess["refs"][rp],
                                                 sess["levels"][rp], sess["ltas"][rp], inst, opts,
                                                 progress, preview_lock)
            rec["status"] = "done"
        except Exception as e:
            traceback.print_exc()
            rec["status"], rec["message"] = "error", f"No he podido calcular el ajuste: {e}"
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/rerender", methods=["POST"])
def api_rerender():
    """Vuelve a tocar las mismas notas con otro sonido o efectos (sin volver a analizar)."""
    data = request.json or {}
    old = jobs.get(data.get("id"))
    if not old or "notes" not in old:
        return jsonify({"error": "Ese trabajo ya no existe; vuelve a convertir el archivo."}), 404
    opts = data.get("options", {})
    sid = opts.get("sound", "escenario")
    same_analysis = all(opts.get(k) == old["opts"].get(k) for k in ("sensitivity", "min_note_ms", "mode", "min_freq", "remove_percussion", "clean"))
    job = new_job(source=old["source"], basename=f"{old['source']} - guitarra flamenca ({instrument.SOUNDS[sid]['name']})")
    job["input"] = os.path.join(job["dir"], "original.wav")
    shutil.copy(old["input"], job["input"])
    reuse = (old["notes"], old["info"]) if same_analysis else None
    threading.Thread(target=run_job, args=(job, opts, reuse), daemon=True).start()
    return jsonify({"id": job["id"]})


@app.route("/api/job/<jid>")
def api_job(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify({"error": "Trabajo no encontrado."}), 404
    out = {k: job.get(k) for k in ("id", "status", "progress", "message", "info", "basename", "sound", "source")}
    if job["status"] == "done":
        out["notes"] = [[round(n["start"], 3), round(n["end"], 3), n["pitch"], n.get("vel", 100)]
                        for n in job["notes"]]
        out["files"] = list(job["files"])
    return jsonify(out)


@app.route("/api/job/<jid>/<kind>")
def api_job_file(jid, kind):
    job = jobs.get(jid)
    if not job:
        abort(404)
    if kind == "original":
        path = job["input"]
    else:
        path = (job.get("files") or {}).get(kind)
    if not path or not os.path.exists(path):
        abort(404)
    dl = request.args.get("download") == "1"
    name = os.path.basename(path) if kind != "original" else job["source"] + ".wav"
    if kind == "mix":
        name = os.path.basename(path)
    mime = "audio/midi" if path.endswith(".mid") else "audio/wav"
    return send_file(path, mimetype=mime, as_attachment=dl, download_name=name, conditional=True)


# ------------------------------------------------------------------ mezclador

def _load_track(path):
    """Lee una pista como estéreo float32 a 44.1 kHz."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2]
    if sr != instrument.SR:
        g = math.gcd(int(sr), instrument.SR)
        data = resample_poly(data, instrument.SR // g, int(sr) // g, axis=0).astype(np.float32)
    return data


@app.route("/api/mix", methods=["POST"])
def api_mix():
    """Mezcla varias pistas (guitarra, original, notas MIDI…) en un WAV final."""
    data = request.json or {}
    tracks = data.get("tracks", [])
    if any(t.get("solo") for t in tracks):
        tracks = [t for t in tracks if t.get("solo")]
    tracks = [t for t in tracks if not t.get("mute") and float(t.get("gain", 1)) > 0]
    if not tracks:
        return jsonify({"error": "Todas las pistas están en silencio."}), 400
    srcs = []
    for t in tracks:
        job = jobs.get(t.get("job"))
        kind = t.get("kind")
        if not job:
            return jsonify({"error": "Una de las pistas ya no existe; vuelve a convertir."}), 400
        path = job["input"] if kind == "original" else (job.get("files") or {}).get(kind)
        if not path or not path.endswith(".wav") or not os.path.exists(path):
            return jsonify({"error": f"No encuentro la pista «{t.get('label', kind)}»."}), 400
        srcs.append((path, float(t.get("gain", 1)), float(t.get("pan", 0)), float(t.get("offset", 0))))
    try:
        with work_lock:
            parts = []
            for path, gain, pan, offset in srcs:
                x = _load_track(path)
                # panorama de potencia constante
                a = (np.clip(pan, -1, 1) + 1) * np.pi / 4
                x = x * np.array([np.cos(a), np.sin(a)], np.float32) * np.sqrt(2) * gain
                pad = int(max(0.0, offset) * instrument.SR)
                parts.append((pad, x))
            n = max(p + len(x) for p, x in parts)
            mix = np.zeros((n, 2), np.float32)
            for p, x in parts:
                mix[p:p + len(x)] += x
            mix *= float(data.get("master", 1.0))
            peak = float(np.abs(mix).max()) or 1.0
            if data.get("normalize", True) or peak > 0.999:
                mix *= 10 ** (-1 / 20) / peak  # -1 dBFS, sin saturar
            first = jobs[tracks[0]["job"]]
            mj = new_job(source=first["source"], basename=first["source"] + " - mezcla")
            out = os.path.join(mj["dir"], f"{safe_name(first['source'])} - mezcla.wav")
            engine.write_wav(mix, out, bits=data.get("bits", 24))
            mj.update(status="done", progress=100, message="Mezcla lista", files={"mix": out},
                      input=out, notes=[], info={})
        return jsonify({"id": mj["id"], "duration": round(n / instrument.SR, 2),
                        "clipped": peak > 0.999 and not data.get("normalize", True)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No he podido mezclar: {e}"}), 500


# ------------------------------------------------------------------ guardar

_TK_PICKER = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
p = filedialog.askdirectory(title=sys.argv[1], initialdir=sys.argv[2], mustexist=False)
sys.stdout.write(p or "")
"""


def pick_folder(prompt, start):
    """Abre el selector de carpetas del sistema. Devuelve la ruta o "" si se cancela."""
    if not os.path.isdir(start):
        start = os.path.expanduser("~")
    if SYSTEM == "Darwin":
        script = (f'set f to choose folder with prompt "{prompt}" '
                  f'default location (POSIX file "{start}")\nPOSIX path of f')
        try:
            r = subprocess.run(["osascript", "-e", 'tell application "System Events" to activate',
                                "-e", script], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:  # sin permiso para System Events: lo intentamos sin él
                r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return ""
        return r.stdout.strip().rstrip("/") if r.returncode == 0 else ""
    # Windows y Linux: el diálogo de tkinter (viene con Python), en un proceso aparte
    try:
        r = subprocess.run([sys.executable, "-c", _TK_PICKER, prompt, start],
                           capture_output=True, text=True, timeout=600, encoding="utf-8")
    except Exception:
        return ""
    return os.path.normpath(r.stdout.strip()) if r.stdout.strip() else ""


@app.route("/api/choose-folder", methods=["POST"])
def api_choose_folder():
    """Abre el selector de carpetas y devuelve la ruta elegida.
    Con {"purpose": "plugin"} elige la carpeta del plugin."""
    purpose = (request.json or {}).get("purpose") if request.is_json else None
    cfg = load_config()
    if purpose == "plugin":
        folder = pick_folder("Elige la carpeta Flamenco Dreams Guitar V2", os.path.expanduser("~"))
        return jsonify({"folder": folder} if folder else {"cancelled": True})
    folder = pick_folder("¿Dónde guardo la guitarra flamenca?", cfg["last_folder"])
    if not folder:
        return jsonify({"cancelled": True})
    save_config(last_folder=folder)
    return jsonify({"folder": folder})


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.json or {}
    job = jobs.get(data.get("id"))
    if not job or job.get("status") != "done":
        return jsonify({"error": "No hay ninguna conversión terminada que guardar."}), 400
    folder = os.path.expanduser((data.get("folder") or "").strip())
    if not folder:
        return jsonify({"error": "Elige primero una carpeta."}), 400
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        return jsonify({"error": f"No puedo crear la carpeta {folder}"}), 400
    name = safe_name(data.get("name") or job["basename"])
    saved = []
    for kind in data.get("formats", ["wav", "mid"]):
        if kind == "mix":
            mix = jobs.get(data.get("mix_id"))
            if not mix or "mix" not in mix.get("files", {}):
                return jsonify({"error": "Primero hay que exportar la mezcla."}), 400
            dst = _unique(os.path.join(folder, f"{name} - mezcla.wav"))
            shutil.copy(mix["files"]["mix"], dst)
            saved.append(dst)
            continue
        if kind not in ("wav", "mid"):
            continue
        dst = _unique(os.path.join(folder, f"{name}.{kind}"))
        shutil.copy(job["files"][kind], dst)
        saved.append(dst)
    save_config(last_folder=folder)
    return jsonify({"saved": saved})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    path = (request.json or {}).get("path", "")
    if os.path.exists(path):
        if SYSTEM == "Windows":
            subprocess.run(["explorer", "/select,", os.path.normpath(path)] if os.path.isfile(path)
                           else ["explorer", os.path.normpath(path)])
        elif SYSTEM == "Darwin":
            subprocess.run(["open", "-R", path] if os.path.isfile(path) else ["open", path])
        else:
            subprocess.run(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])
    return jsonify({"ok": True})


@app.route("/api/quit", methods=["POST"])
def api_quit():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


# ------------------------------------------------------------------ arranque

def clean_old_jobs(max_age_h=24):
    if not os.path.isdir(JOBS_DIR):
        return
    now = time.time()
    for d in os.listdir(JOBS_DIR):
        p = os.path.join(JOBS_DIR, d)
        if os.path.isdir(p) and now - os.path.getmtime(p) > max_age_h * 3600:
            shutil.rmtree(p, ignore_errors=True)


def free_port():
    for p in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("No hay ningún puerto libre entre 8850 y 8859.")


def warmup():
    try:
        get_instrument()
        engine.get_model()
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    clean_old_jobs()
    port = free_port()
    with open(os.path.join(HERE, ".port"), "w") as f:
        f.write(str(port))
    with open(os.path.join(HERE, ".pid"), "w") as f:
        f.write(str(os.getpid()))
    threading.Thread(target=warmup, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"Guitarra Flamenca en {url}", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
