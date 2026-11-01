"""Prueba rápida de punta a punta (sin necesitar el plugin real).

Crea un plugin de imitación (guitarra sintética en formato Decent Sampler),
un WAV con acordes, y recorre la app como lo haría el navegador:
configuración → preescucha en directo → ajuste recomendado → convertir →
descargar WAV/MIDI → mezclar → guardar.

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


def g_voicings(cr, vo):
    """Posturas alineadas con todos los segmentos (los «sin acorde» no llevan)."""
    it = iter(vo["voicings"])
    return [None if x["label"] == "N" else next(it, None) for x in cr["segments"]]


def cfg_ids(cfg):
    return {x["id"] for x in cfg["sounds"]}


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
    server.PRESETS = os.path.join(TMP, "mis_ajustes.json")
    c = server.app.test_client()

    print("1. Configuración")
    cfg = c.get("/api/config").get_json()
    check(cfg["plugin_ok"], f"plugin encontrado en {cfg['plugin_dir']}")
    check(len(cfg["sounds"]) >= 4, f"{len(cfg['sounds'])} sonidos disponibles")
    check(c.get("/").status_code == 200, "la página carga")
    check(c.get("/api/demo/estudio").status_code == 200, "demo de sonido")

    print("1b. Mis ajustes guardados")
    mine = {"sound": "escenario", "reverb": 0.42, "eq_low": 2, "strum": 0.6}
    r = c.post("/api/presets", json={"name": "  Voz con  tablao ", "options": mine}).get_json()
    check([p["name"] for p in r["presets"]] == ["Voz con tablao"], "guardados con su nombre")
    got = c.get("/api/presets").get_json()["presets"][0]["options"]
    check(got == mine, "se recuperan exactamente iguales")
    r = c.post("/api/presets/delete", json={"name": "Voz con tablao"}).get_json()
    check(r["presets"] == [], "se pueden borrar")

    print("2. Preescucha en directo")
    r = c.post("/api/preview/start", data={"file": (test_wav(), "prueba acordes.wav"), "options": "{}"},
               content_type="multipart/form-data").get_json()
    pid = r["id"]
    while (st := c.get(f"/api/preview/{pid}").get_json())["status"] not in ("ready", "error"):
        time.sleep(0.3)
    check(st["status"] == "ready", f"preescucha preparada ({st['duration']:.1f} s de audio)")
    for k, opts in enumerate(({"sound": "estudio"}, {"sound": "escenario", "reverb": 0.5, "sensitivity": 0.7})):
        t0 = time.time()
        pr = c.post(f"/api/preview/{pid}/play", json={"options": opts, "start": 0, "length": 5, "serial": k + 1})
        a, sr = sf.read(io.BytesIO(pr.data))
        check(pr.status_code == 200 and abs(len(a) / sr - 5) < 0.05 and np.abs(a).max() > 0.01,
              f"fragmento de {len(a) / sr:.1f} s con {pr.headers['X-Notes']} notas en {time.time() - t0:.1f} s")

    print("3. Ajuste recomendado")
    t0 = time.time()
    c.post(f"/api/preview/{pid}/recommend", json={"options": {"sound": "estudio"}})
    while (rc := c.get(f"/api/preview/{pid}/recommend").get_json())["status"] == "running":
        time.sleep(0.5)
    check(rc["status"] == "done", f"calculado en {time.time() - t0:.1f} s ({rc.get('message')})")
    res = rc["result"]
    check(res["options"]["sound"] in cfg_ids(cfg) and len(res["reasons"]) >= 3,
          f"recomienda «{res['options']['sound']}» con {len(res['reasons'])} explicaciones")
    check(res["after"]["total"] >= res["before"]["total"] - 1e-9,
          f"parecido {res['before']['total']:.3f} → {res['after']['total']:.3f} (no empeora)")

    print("3b. Acordes y guitarra rítmica")
    t0 = time.time()
    c.post(f"/api/preview/{pid}/chords")
    while (ch := c.get(f"/api/preview/{pid}/chords").get_json())["status"] == "running":
        time.sleep(0.3)
    check(ch["status"] == "done", f"acordes detectados en {time.time() - t0:.1f} s")
    cr = ch["result"]
    labels = [x["label"] for x in cr["segments"] if x["label"] != "N"]
    want = ["A:min", "G:maj", "F:maj", "E:maj"]
    found = [w for w in want if w in labels]
    print(f"     {' '.join(labels)} · {cr['key']['name']} · {cr['tempo']:.0f} BPM")
    check(len(found) >= 3, f"reconoce {len(found)}/4 acordes de la cadencia andaluza")
    vo = c.post("/api/chords/voicings", json={"labels": labels, "capo": "auto"}).get_json()
    check(len(vo["voicings"]) == len(labels) and all(v and len(v["frets"]) == 6 for v in vo["voicings"]),
          f"posturas de guitarra (cejilla {vo['capo']}): " + ", ".join(v["name"] for v in vo["voicings"][:4]))
    t0 = time.time()
    rj = c.post("/api/chords/render", json={"sid": pid, "segments": cr["segments"], "beats": cr["beats"],
                                           "downbeats": cr["downbeats"], "tempo": cr["tempo"], "name": "prueba",
                                           "tuning_cents": cr["tuning_cents"], "rhythm": {"pattern": "rumba"},
                                           "options": {"sound": "estudio"}}).get_json()
    while (rjob := c.get(f"/api/job/{rj['id']}").get_json())["status"] not in ("done", "error"):
        time.sleep(0.3)
    check(rjob["status"] == "done", f"guitarra rítmica generada en {time.time() - t0:.1f} s ({rjob['info']['n_notes']} notas)")
    ra, rsr = sf.read(io.BytesIO(c.get(f"/api/job/{rj['id']}/wav").data))
    check(ra.ndim == 2 and len(ra) / rsr > 4 and np.isfinite(ra).all() and np.abs(ra).max() > 0.05, f"WAV de {len(ra) / rsr:.1f} s")
    check(c.get(f"/api/job/{rj['id']}/mid").data[:4] == b"MThd", "MIDI de la guitarra rítmica")
    ev = cr.get("events") or []
    check(len(ev) >= 3, f"golpes del original: {len(ev)} ({', '.join(sorted({e['kind'] for e in ev}))})")
    ro = c.post("/api/chords/render", json={"sid": pid, "segments": cr["segments"], "beats": cr["beats"],
                                           "downbeats": cr["downbeats"], "tempo": cr["tempo"], "name": "prueba",
                                           "tuning_cents": cr["tuning_cents"], "rhythm": {"pattern": "original"},
                                           "options": {"sound": "estudio"}}).get_json()
    while (rjo := c.get(f"/api/job/{ro['id']}").get_json())["status"] not in ("done", "error"):
        time.sleep(0.3)
    check(rjo["status"] == "done", f"guitarra rítmica copiando el ritmo del original ({rjo.get('info', {}).get('n_notes')} notas)")

    print("3c. Exportar acordes (PDF y vídeo backtrack)")
    exp_dir = os.path.join(TMP, "exportes con espacios")
    edata = {"title": "Prueba", "key_name": cr["key"]["name"], "tempo": cr["tempo"], "meter": cr["meter"],
             "capo": vo["capo"], "flats": cr["key"]["flats"], "notation": "es", "duration": cr["duration"],
             "segments": cr["segments"], "voicings": g_voicings(cr, vo), "beats": cr["beats"], "downbeats": cr["downbeats"]}
    r = c.post("/api/chords/export", json={"kind": "pdf", "folder": exp_dir, "name": "Prueba", "data": edata}).get_json()
    check("saved" in r and open(r["saved"], "rb").read(5) == b"%PDF-", f"PDF: {os.path.basename(r.get('saved', '?'))}")
    for q in ("720", "whatsapp"):
        t0 = time.time()
        r = c.post("/api/chords/export", json={"kind": "video", "sid": pid, "folder": exp_dir, "name": "Prueba",
                                               "data": edata, "quality": q, "include_rhythm": True,
                                               "rhythm_job": ro["id"]}).get_json()
        while (vj := c.get(f"/api/job/{r['id']}").get_json())["status"] not in ("done", "error"):
            time.sleep(0.3)
        ok = vj["status"] == "done" and os.path.getsize(vj["info"]["saved"]) > 20000
        check(ok, f"vídeo {vj.get('info', {}).get('quality', q)}: {vj.get('info', {}).get('mb')} MB en {time.time() - t0:.1f} s ({vj.get('message')})")

    print("4. Conversión (reutiliza el análisis de la preescucha)")
    t0 = time.time()
    r = c.post("/api/convert", data={"file": (test_wav(), "prueba acordes.wav"), "preview_id": pid,
                                     "options": json.dumps(res["options"])},
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

    print("5. Archivos")
    w = c.get(f"/api/job/{jid}/wav")
    audio, sr = sf.read(io.BytesIO(w.data))
    check(w.status_code == 200 and audio.ndim == 2 and len(audio) > sr * 4, f"WAV estéreo de {len(audio) / sr:.1f} s")
    check(np.isfinite(audio).all() and 0.05 < np.abs(audio).max() <= 1.0, f"nivel correcto (pico {np.abs(audio).max():.2f})")
    m = c.get(f"/api/job/{jid}/mid")
    check(m.status_code == 200 and m.data[:4] == b"MThd", "MIDI válido")

    print("6. Mezclador")
    mx = c.post("/api/mix", json={"tracks": [{"job": jid, "kind": "wav", "gain": 1},
                                             {"job": jid, "kind": "original", "gain": 0.3},
                                             {"job": rj["id"], "kind": "wav", "gain": 0.8}]}).get_json()
    check("id" in mx, f"mezcla de {mx.get('duration')} s")

    print("7. Guardar")
    out = os.path.join(TMP, "salida con espacios y ñ")
    s = c.post("/api/save", json={"id": jid, "folder": out, "name": "Prueba (1)",
                                  "formats": ["wav", "mid", "mix"], "mix_id": mx["id"]}).get_json()
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    check(len(files) == 3, f"guardados: {files}")
    s2 = c.post("/api/save", json={"id": rj["id"], "folder": out, "name": "Prueba rítmica",
                                   "formats": ["wav", "mid"]}).get_json()
    check(len(s2.get("saved", [])) == 2, "guitarra rítmica guardada (WAV + MIDI)")

    print("\nTODO CORRECTO ✅")


if __name__ == "__main__":
    main()
