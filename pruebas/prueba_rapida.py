"""Prueba rápida de punta a punta (sin necesitar el plugin real).

Crea un plugin de imitación (guitarra sintética en formato Decent Sampler),
un WAV con acordes, y recorre la app como lo haría el navegador:
configuración → convertir → descargar WAV/MIDI → mezclar → guardar.

Uso:  python pruebas/prueba_rapida.py      (sale con código 0 si todo va bien)
"""
import io
import json
import os
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SR = 44100
TMP = tempfile.mkdtemp(prefix="guitarra_prueba_")


def pluck(pitch, seconds=2.5, seed=0):
    """Cuerda pulsada (Karplus-Strong): suena parecido a una guitarra."""
    rng = np.random.default_rng(seed)
    f = 440 * 2 ** ((pitch - 69) / 12)
    n = int(SR / f)
    buf = rng.uniform(-1, 1, n)
    out = np.zeros(int(seconds * SR))
    for i in range(len(out)):
        out[i] = buf[i % n]
        buf[i % n] = 0.996 * 0.5 * (buf[i % n] + buf[(i + 1) % n])
    return (out / np.abs(out).max() * 0.8).astype(np.float32)


def fake_plugin():
    d = os.path.join(TMP, "Flamenco Dreams Guitar V2")
    os.makedirs(os.path.join(d, "Samples"))
    lines = []
    for root in range(40, 85, 4):
        for rr in (1, 2):
            name = f"Samples/s_{root}_{rr}.wav"
            sf.write(os.path.join(d, name), np.repeat(pluck(root, seed=rr)[:, None], 2, 1), SR)
            lines.append(f'<sample rootNote="{root}" loNote="{root}" hiNote="{root + 3}" '
                         f'seqPosition="{rr}" path="{name}" />')
    s = "\n".join(lines)
    v2 = (f'<DecentSampler><groups>'
          f'<group volume="7.0dB" name="StudioGuitar" tags="StudioGuitar">{s}</group>'
          f'<group volume="4.0dB" name="StageGuitar" tags="StageGuitar">{s}</group>'
          f'</groups></DecentSampler>')
    v1 = f'<DecentSampler><groups><group seqMode="round_robin">{s}</group></groups></DecentSampler>'
    with open(os.path.join(d, "Flamenco Dreams Guitar V2.dspreset"), "w", encoding="utf-8") as f:
        f.write(v2)
    with open(os.path.join(d, "Flamenco Dreams Guitar V1.dspreset"), "w", encoding="utf-8") as f:
        f.write(v1)
    return d


def test_wav():
    """Cadencia andaluza con sinusoides (La m - Sol - Fa - Mi), 6 s."""
    chords = [[57, 60, 64], [55, 59, 62], [53, 57, 60], [52, 56, 59]]
    y = np.zeros(int(6.2 * SR))
    t = np.arange(int(1.4 * SR)) / SR
    for i, ch in enumerate(chords):
        for p in ch:
            f = 440 * 2 ** ((p - 69) / 12)
            tone = sum(np.sin(2 * np.pi * f * k * t) / k for k in (1, 2, 3)) * np.exp(-t * 1.5)
            s = int(i * 1.5 * SR)
            y[s:s + len(t)] += tone * np.minimum(1, t / 0.005)
    y = y / np.abs(y).max() * 0.8
    buf = io.BytesIO()
    sf.write(buf, y.astype(np.float32), SR, format="WAV")
    buf.seek(0)
    return buf


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def main():
    plugin = fake_plugin()
    os.environ["FLAMENCO_PLUGIN_DIR"] = plugin
    import server
    server.CONFIG = os.path.join(TMP, "config.json")  # no toca la configuración del usuario
    server.JOBS_DIR = os.path.join(TMP, "trabajos")
    server.CACHE_DIR = os.path.join(TMP, "cache")
    c = server.app.test_client()

    print("1. Configuración")
    cfg = c.get("/api/config").get_json()
    check(cfg["plugin_ok"], f"plugin encontrado en {cfg['plugin_dir']}")
    check(len(cfg["sounds"]) >= 4, f"{len(cfg['sounds'])} sonidos disponibles")
    check(c.get("/").status_code == 200, "la página carga")
    check(c.get("/api/demo/estudio").status_code == 200, "demo de sonido")

    print("2. Conversión")
    t0 = time.time()
    r = c.post("/api/convert", data={"file": (test_wav(), "prueba acordes.wav"),
                                     "options": json.dumps({"sound": "estudio"})},
               content_type="multipart/form-data").get_json()
    check("id" in r, "trabajo creado")
    jid = r["id"]
    while True:
        j = c.get(f"/api/job/{jid}").get_json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    check(j["status"] == "done", f"conversión terminada en {time.time() - t0:.1f} s ({j.get('message')})")
    pitches = sorted({n[2] for n in j["notes"]})
    print(f"     notas detectadas: {len(j['notes'])}  alturas: {pitches}")
    expected = {57, 60, 64, 55, 59, 62, 53, 52, 56}
    hit = len(expected & set(pitches)) / len(expected)
    check(hit >= 0.6, f"reconoce {hit:.0%} de las notas de los acordes")

    print("3. Archivos")
    w = c.get(f"/api/job/{jid}/wav")
    audio, sr = sf.read(io.BytesIO(w.data))
    check(w.status_code == 200 and audio.ndim == 2 and len(audio) > sr * 4, f"WAV estéreo de {len(audio) / sr:.1f} s")
    check(np.isfinite(audio).all() and 0.05 < np.abs(audio).max() <= 1.0, f"nivel correcto (pico {np.abs(audio).max():.2f})")
    m = c.get(f"/api/job/{jid}/mid")
    check(m.status_code == 200 and m.data[:4] == b"MThd", "MIDI válido")

    print("4. Mezclador")
    mx = c.post("/api/mix", json={"tracks": [{"job": jid, "kind": "wav", "gain": 1},
                                             {"job": jid, "kind": "original", "gain": 0.3}]}).get_json()
    check("id" in mx, f"mezcla de {mx.get('duration')} s")

    print("5. Guardar")
    out = os.path.join(TMP, "salida con espacios y ñ")
    s = c.post("/api/save", json={"id": jid, "folder": out, "name": "Prueba (1)",
                                  "formats": ["wav", "mid", "mix"], "mix_id": mx["id"]}).get_json()
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    check(len(files) == 3, f"guardados: {files}")

    print("\nTODO CORRECTO ✅")


if __name__ == "__main__":
    main()
