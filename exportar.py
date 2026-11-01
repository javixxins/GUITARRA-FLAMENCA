"""Exportar los acordes para aprender la canción:
- vídeo backtrack horizontal (MP4 H.264 + AAC) con el acorde actual y su diagrama, el siguiente
  con cuenta atrás, los pulsos del compás y la línea de tiempo, sincronizados con el audio;
- PDF horizontal con los diagramas de los acordes y la partitura de acordes compás a compás.

Funciona igual en Windows y macOS: ffmpeg viene con imageio-ffmpeg, el PDF se hace con reportlab
y la tipografía (DejaVu, licencia libre) va con la app.
"""
import os
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import acordes

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fuentes")
F_REG = os.path.join(FONT_DIR, "DejaVuSans.ttf")
F_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
F_SERIF = os.path.join(FONT_DIR, "DejaVuSerif-Bold.ttf")

BG = (18, 13, 10)
PANEL = (34, 25, 20)
LINE = (58, 43, 32)
TEXT = (243, 233, 221)
MUTED = (185, 166, 144)
GOLD = (209, 157, 97)
GOLD2 = (232, 184, 120)
QNAME = {"maj": "mayor", "min": "menor", "7": "séptima", "maj7": "séptima mayor", "min7": "menor séptima",
         "dim": "disminuido", "sus4": "suspendido 4ª"}


def names(seg, flats, notation):
    """(nombre principal, nombre secundario) del acorde de un segmento."""
    r, q = seg.get("root"), seg.get("quality")
    en, es = acordes.chord_names(r, q, flats)
    return (es, en) if notation == "es" else (en, es)


def chord_color(root, q):
    if root is None:
        return (42, 33, 27)
    import colorsys
    h = ((root * 30 + 20) % 360) / 360
    lum = 0.58 if q in ("min", "min7") else 0.50 if q == "dim" else 0.66
    r, g, b = colorsys.hls_to_rgb(h, lum, 0.55)
    return int(r * 255), int(g * 255), int(b * 255)


def fmt_t(t):
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


# ---------------------------------------------------------------- diagrama (imagen)

def draw_diagram(d, x, y, v, scale=1.0, color=GOLD2):
    """Diagrama de guitarra (6 cuerdas, 5 trastes) con su postura, en (x, y)."""
    if not v:
        return
    fr = v["frets"]
    used = [f for f in fr if f > 0]
    base = min(used) if used and max(used) > 4 else 1
    sx, sy = 26 * scale, 34 * scale
    x0, y0 = x + 20 * scale, y + 40 * scale
    w = max(1, int(2 * scale))
    for i in range(6):
        d.line([(x0 + i * sx, y0), (x0 + i * sx, y0 + 5 * sy)], fill=MUTED, width=max(1, int((2.4 if i < 3 else 1.6) * scale)))
    for j in range(6):
        d.line([(x0, y0 + j * sy), (x0 + 5 * sx, y0 + j * sy)], fill=(122, 101, 80),
               width=int(7 * scale) if j == 0 and base == 1 else w)
    f_small = ImageFont.truetype(F_BOLD, int(20 * scale))
    if base > 1:
        d.text((x0 + 5 * sx + 10 * scale, y0 + sy * 0.15), str(base), font=f_small, fill=MUTED)
    for i, f in enumerate(fr):
        cx = x0 + i * sx
        if f < 0:
            d.text((cx, y0 - 20 * scale), "×", font=f_small, fill=MUTED, anchor="mm")
        elif f == 0:
            r = 8 * scale
            d.ellipse([cx - r, y0 - 20 * scale - r, cx + r, y0 - 20 * scale + r], outline=color, width=max(1, int(2.5 * scale)))
        else:
            r = 10 * scale
            cy = y0 + (f - base + 0.5) * sy
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    if v.get("capo"):
        d.text((x0 + 2.5 * sx, y0 + 5 * sy + 26 * scale), f"cejilla {v['capo']}", font=f_small, fill=color, anchor="mm")


# ---------------------------------------------------------------- vídeo

class _Fonts:
    def __init__(self, s):
        self.title = ImageFont.truetype(F_SERIF, int(46 * s))
        self.info = ImageFont.truetype(F_REG, int(28 * s))
        self.label = ImageFont.truetype(F_BOLD, int(26 * s))
        self.huge = ImageFont.truetype(F_SERIF, int(250 * s))
        self.big = ImageFont.truetype(F_SERIF, int(130 * s))
        self.sub = ImageFont.truetype(F_REG, int(34 * s))
        self.block = ImageFont.truetype(F_BOLD, int(54 * s))
        self.small = ImageFont.truetype(F_REG, int(22 * s))
        self.mid = ImageFont.truetype(F_BOLD, int(30 * s))


def _fit(d, text, font_path, size, max_w):
    f = ImageFont.truetype(font_path, size)
    while d.textlength(text, font=f) > max_w and size > 20:
        size = int(size * 0.9)
        f = ImageFont.truetype(font_path, size)
    return f


QUALITIES = {
    "1080": {"name": "Full HD 1080p", "width": 1920, "crf": 18, "audio": "192k"},
    "720": {"name": "HD 720p", "width": 1280, "crf": 21, "audio": "160k"},
    # WhatsApp: 960×540, H.264 main, AAC; el peso se calcula para quedar por debajo de 16 MB
    "whatsapp": {"name": "WhatsApp", "width": 960, "target_mb": 15.0, "audio": "96k"},
}


def render_video(data, audio, out_path, progress=lambda *a: None, width=None, fps=25, quality="1080"):
    """Crea el MP4. `audio` = [(ruta_wav, ganancia)], el primero es el original."""
    import imageio_ffmpeg
    qd = QUALITIES.get(quality, QUALITIES["1080"])
    width = width or qd["width"]
    W, H = int(width), int(round(width * 9 / 16 / 2) * 2)
    s = W / 1920
    F = _Fonts(s)
    segs = data["segments"]
    voic = data.get("voicings") or [None] * len(segs)
    flats, nota = bool(data.get("flats")), data.get("notation", "es")
    dur = float(data["duration"])
    beats = np.asarray(data.get("beats") or [], float)
    downs = np.asarray(data.get("downbeats") or [], float)
    meter = int(data.get("meter") or 4)
    starts = np.array([x["start"] for x in segs])

    # --- fondo fijo (cabecera)
    bg = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(bg)
    for yy in range(H):  # degradado suave
        k = yy / H
        d.line([(0, yy), (W, yy)], fill=(int(18 + 14 * (1 - k)), int(13 + 8 * (1 - k)), int(10 + 4 * (1 - k))))
    d.text((60 * s, 40 * s), data.get("title") or "Acordes", font=_fit(d, data.get("title") or "Acordes", F_SERIF, int(46 * s), W * 0.5), fill=TEXT)
    info = f"{data.get('key_name', '')}  ·  {round(float(data.get('tempo') or 0))} BPM  ·  compás de {meter}" + \
        (f"  ·  cejilla {data['capo']}" if data.get("capo") else "")
    d.text((W - 60 * s, 52 * s), info, font=F.info, fill=MUTED, anchor="ra")
    d.line([(60 * s, 118 * s), (W - 60 * s, 118 * s)], fill=LINE, width=max(1, int(2 * s)))

    # --- tira de la línea de tiempo (se dibuja una vez y se recorta en cada fotograma)
    PX = 260 * s
    strip_h = int(190 * s)
    head_x = int(W * 0.3)
    pad_l, pad_r = head_x, W - head_x
    strip = Image.new("RGB", (int(dur * PX) + pad_l + pad_r + 2, strip_h), (13, 9, 7))
    sd = ImageDraw.Draw(strip)
    for b in beats:
        x = pad_l + b * PX
        sd.line([(x, 0), (x, 16 * s)], fill=(90, 70, 52), width=max(1, int(2 * s)))
    for i, b in enumerate(downs):
        x = pad_l + b * PX
        sd.line([(x, 0), (x, strip_h)], fill=(110, 86, 62), width=max(1, int(2 * s)))
        sd.text((x + 6 * s, 2 * s), str(i + 1), font=F.small, fill=MUTED)
    for x_ in segs:
        x1, x2 = pad_l + x_["start"] * PX, pad_l + x_["end"] * PX
        c = chord_color(x_.get("root"), x_.get("quality"))
        sd.rounded_rectangle([x1 + 2 * s, 30 * s, x2 - 2 * s, strip_h - 10 * s], radius=int(12 * s), fill=c,
                             outline=(0, 0, 0))
        if x_.get("root") is not None:
            nm = names(x_, flats, nota)[0]
            f = _fit(sd, nm, F_BOLD, int(54 * s), max(10, x2 - x1 - 24 * s))
            sd.text((x1 + 14 * s, 44 * s), nm, font=f, fill=(26, 18, 12))

    # --- panel de cada acorde (actual + siguiente), en caché
    cache = {}

    def panel(i):
        if i in cache:
            return cache[i]
        img = Image.new("RGBA", (W, int(560 * s)), (0, 0, 0, 0))
        p = ImageDraw.Draw(img)
        p.rounded_rectangle([60 * s, 0, 1120 * s, 540 * s], radius=int(24 * s), fill=PANEL + (255,))
        p.rounded_rectangle([1160 * s, 0, W - 60 * s, 540 * s], radius=int(24 * s), fill=PANEL + (255,))
        p.text((100 * s, 30 * s), "AHORA", font=F.label, fill=MUTED)
        p.text((1200 * s, 30 * s), "SIGUIENTE", font=F.label, fill=MUTED)
        cur = segs[i] if 0 <= i < len(segs) else None
        if cur is not None and cur.get("root") is not None:
            a, b = names(cur, flats, nota)
            p.text((100 * s, 80 * s), a, font=_fit(p, a, F_SERIF, int(250 * s), 640 * s), fill=GOLD2)
            p.text((104 * s, 400 * s), f"{b} · {QNAME.get(cur.get('quality'), '')}", font=F.sub, fill=MUTED)
            vv = voic[i] if i < len(voic) else None
            if vv:
                p.text((104 * s, 448 * s), vv.get("name", ""), font=F.small, fill=MUTED)
                draw_diagram(p, 790 * s, 70 * s, vv, scale=1.85 * s)
        else:
            p.text((100 * s, 140 * s), "—", font=F.huge, fill=MUTED)
        nxt = next((k for k in range(i + 1, len(segs)) if segs[k].get("root") is not None), None)
        if nxt is not None:
            a, b = names(segs[nxt], flats, nota)
            p.text((1200 * s, 80 * s), a, font=_fit(p, a, F_SERIF, int(130 * s), 420 * s), fill=TEXT)
            p.text((1204 * s, 250 * s), b, font=F.sub, fill=MUTED)
            vv = voic[nxt] if nxt < len(voic) else None
            if vv:
                draw_diagram(p, 1600 * s, 50 * s, vv, scale=1.25 * s, color=TEXT)
        cache[i] = (img, nxt)
        if len(cache) > 64:
            cache.pop(next(iter(cache)))
        return cache[i]

    # --- ffmpeg: vídeo por la entrada estándar + audio
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-"]
    for path, _ in audio:
        cmd += ["-i", path]
    if len(audio) == 1:
        cmd += ["-map", "0:v", "-map", "1:a", "-filter:a", f"volume={audio[0][1]:.3f}"]
    else:
        parts = "".join(f"[{k + 1}:a]volume={g:.3f}[a{k}];" for k, (_, g) in enumerate(audio))
        mixin = "".join(f"[a{k}]" for k in range(len(audio)))
        cmd += ["-filter_complex", f"{parts}{mixin}amix=inputs={len(audio)}:normalize=0,alimiter=limit=0.95[a]",
                "-map", "0:v", "-map", "[a]"]
    if "target_mb" in qd:
        abr = int(qd["audio"].rstrip("k")) * 1000
        vbr = int(np.clip(qd["target_mb"] * 8e6 / max(1.0, dur) - abr - 20000, 150000, 1500000))
        cmd += ["-c:v", "libx264", "-preset", "medium", "-profile:v", "main", "-level", "3.1",
                "-b:v", str(vbr), "-maxrate", str(int(vbr * 1.3)), "-bufsize", str(int(vbr * 2)),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", qd["audio"], "-ac", "2", "-ar", "44100"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(qd["crf"]), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", qd["audio"]]
    cmd += ["-movflags", "+faststart", "-t", f"{dur:.3f}", out_path]
    err_path = out_path + ".log"
    with open(err_path, "w") as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        n_frames = int(np.ceil(dur * fps))
        try:
            for k in range(n_frames):
                t = k / fps
                i = int(np.searchsorted(starts, t + 1e-6, side="right") - 1)
                if i >= 0 and t >= segs[i]["end"]:
                    i = -1 if i == len(segs) - 1 else i
                frame = bg.copy()
                img, nxt = panel(i)
                frame.paste(img, (0, int(150 * s)), img)
                fd = ImageDraw.Draw(frame)
                # cuenta atrás hasta el siguiente acorde
                if nxt is not None:
                    left = segs[nxt]["start"] - t
                    prev = segs[i]["start"] if i >= 0 else 0.0
                    span = max(0.1, segs[nxt]["start"] - prev)
                    frac = float(np.clip(1 - left / span, 0, 1))
                    y = 150 * s + 470 * s
                    fd.rounded_rectangle([1200 * s, y, W - 100 * s, y + 18 * s], radius=int(9 * s), fill=(20, 15, 12))
                    fd.rounded_rectangle([1200 * s, y, 1200 * s + (W - 1300 * s) * frac, y + 18 * s], radius=int(9 * s), fill=GOLD)
                    fd.text((1200 * s, y - 52 * s), f"en {left:.1f} s".replace(".", ","), font=F.mid, fill=GOLD2 if left < 1.5 else MUTED)
                # pulsos del compás
                if len(downs):
                    bi = int(np.searchsorted(downs, t + 1e-6, side="right") - 1)
                    if bi >= 0 and len(beats):
                        b_now = int(np.searchsorted(beats, t + 1e-6, side="right") - 1)
                        b_bar = int(np.searchsorted(beats, downs[bi] - 1e-3))
                        pos = (b_now - b_bar) % meter if b_now >= 0 else -1
                        y = 150 * s + 560 * s + 22 * s
                        fd.text((60 * s, y - 16 * s), f"Compás {bi + 1}", font=F.mid, fill=MUTED)
                        for k2 in range(meter):
                            cx = 330 * s + k2 * 58 * s
                            r = (17 if k2 == 0 else 13) * s
                            on = k2 == pos
                            fd.ellipse([cx - r, y - r + 4 * s, cx + r, y + r + 4 * s], fill=GOLD2 if on else (60, 46, 36))
                # línea de tiempo
                x0 = int(t * PX)
                crop = strip.crop((x0, 0, x0 + W, strip_h))
                ty = int(H - strip_h - 70 * s)
                frame.paste(crop, (0, ty))
                fd.rectangle([head_x - 2 * s, ty - 6 * s, head_x + 2 * s, ty + strip_h + 6 * s], fill=(255, 255, 255))
                # progreso
                py = H - 40 * s
                fd.rounded_rectangle([60 * s, py, W - 260 * s, py + 10 * s], radius=int(5 * s), fill=(40, 30, 24))
                fd.rounded_rectangle([60 * s, py, 60 * s + (W - 320 * s) * min(1, t / dur), py + 10 * s], radius=int(5 * s), fill=GOLD)
                fd.text((W - 60 * s, py - 12 * s), f"{fmt_t(t)} / {fmt_t(dur)}", font=F.info, fill=MUTED, anchor="ra")
                proc.stdin.write(frame.tobytes())
                if k % fps == 0:
                    progress(100 * k / n_frames, f"Creando el vídeo… {fmt_t(t)} de {fmt_t(dur)}")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
    if rc != 0 or not os.path.exists(out_path):
        with open(err_path) as f:
            raise RuntimeError("ffmpeg no ha podido crear el vídeo: " + f.read()[-400:])
    os.remove(err_path)
    progress(100, "Vídeo listo")
    return out_path


# ---------------------------------------------------------------- PDF

def bars_of(data):
    """Compases [(inicio, fin, nº)] a partir de los primeros tiempos (con anacrusa si la hay)."""
    downs = list(data.get("downbeats") or [])
    dur = float(data["duration"])
    if not downs:
        step = 4 * 60.0 / max(40.0, float(data.get("tempo") or 100))
        downs = list(np.arange(0, dur, step))
    out = []
    if downs[0] > 0.25:
        out.append((0.0, downs[0], 0))
    for k, a in enumerate(downs):
        b = downs[k + 1] if k + 1 < len(downs) else dur
        if b - a > 0.05:
            out.append((a, b, k + 1))
    return out


def render_pdf(data, out_path):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for nm, path in (("DV", F_REG), ("DVB", F_BOLD), ("DVS", F_SERIF)):
        try:
            pdfmetrics.registerFont(TTFont(nm, path))
        except Exception:
            pass
    PW, PH = landscape(A4)
    M = 36
    c = canvas.Canvas(out_path, pagesize=(PW, PH))
    title = data.get("title") or "Acordes"
    c.setTitle(f"{title} - acordes")
    c.setAuthor("Guitarra Flamenca")
    segs = data["segments"]
    voic = data.get("voicings") or [None] * len(segs)
    flats, nota = bool(data.get("flats")), data.get("notation", "es")
    meter = int(data.get("meter") or 4)
    page = [1]

    def rgb(col):
        return [x / 255 for x in col]

    def footer():
        c.setFont("DV", 8)
        c.setFillColorRGB(0.45, 0.4, 0.35)
        c.drawString(M, 18, "Generado con Guitarra Flamenca · acordes detectados automáticamente: revísalos de oído")
        c.drawRightString(PW - M, 18, f"Página {page[0]}")

    def header(first):
        c.setFillColorRGB(0.1, 0.07, 0.05)
        if first:
            c.setFont("DVS", 22)
            c.drawString(M, PH - M - 16, title)
            c.setFont("DV", 11)
            c.setFillColorRGB(0.35, 0.3, 0.25)
            info = f"Tonalidad: {data.get('key_name', '')}   ·   Tempo: {round(float(data.get('tempo') or 0))} BPM   ·   " \
                   f"Compás de {meter}   ·   " + (f"Cejilla en el traste {data['capo']}" if data.get("capo") else "Sin cejilla") + \
                   f"   ·   Duración {fmt_t(float(data['duration']))}"
            c.drawString(M, PH - M - 36, info)
            return PH - M - 52
        c.setFont("DVB", 11)
        c.drawString(M, PH - M - 8, f"{title} (continuación)")
        return PH - M - 22

    def diagram(x, y, v, sc=1.0):
        """Diagrama vectorial; (x, y) = esquina superior izquierda."""
        fr = v["frets"]
        used = [f for f in fr if f > 0]
        base = min(used) if used and max(used) > 4 else 1
        sx, sy = 9 * sc, 11 * sc
        x0, y0 = x + 8 * sc, y - 14 * sc
        c.setStrokeColorRGB(0.3, 0.25, 0.2)
        for i in range(6):
            c.setLineWidth(1.0 if i < 3 else 0.6)
            c.line(x0 + i * sx, y0, x0 + i * sx, y0 - 5 * sy)
        for j in range(6):
            c.setLineWidth(2.4 if j == 0 and base == 1 else 0.6)
            c.line(x0, y0 - j * sy, x0 + 5 * sx, y0 - j * sy)
        c.setFont("DV", 7 * sc)
        if base > 1:
            c.setFillColorRGB(0.3, 0.25, 0.2)
            c.drawString(x0 + 5 * sx + 3, y0 - sy * 0.8, str(base))
        for i, f in enumerate(fr):
            cx = x0 + i * sx
            if f < 0:
                c.setFillColorRGB(0.3, 0.25, 0.2)
                c.drawCentredString(cx, y0 + 3 * sc, "×")
            elif f == 0:
                c.setStrokeColorRGB(0.55, 0.36, 0.15)
                c.setLineWidth(0.8)
                c.circle(cx, y0 + 5 * sc, 2.6 * sc, stroke=1, fill=0)
            else:
                c.setFillColorRGB(0.55, 0.36, 0.15)
                c.circle(cx, y0 - (f - base + 0.5) * sy, 3.3 * sc, stroke=0, fill=1)

    # --- 1) los acordes de la canción, con su postura
    y = header(True)
    seen, uniq = set(), []
    for k, x in enumerate(segs):
        if x.get("root") is None or x["label"] in seen:
            continue
        seen.add(x["label"])
        uniq.append((x, voic[k] if k < len(voic) else None))
    c.setFont("DVB", 12)
    c.setFillColorRGB(0.1, 0.07, 0.05)
    c.drawString(M, y - 6, "Acordes de la canción")
    y -= 14
    cell = 74
    per_row = int((PW - 2 * M) // cell)
    for k, (x, v) in enumerate(uniq):
        col, row = k % per_row, k // per_row
        if col == 0 and row > 0:
            y -= 118
        if y < 150:
            footer()
            c.showPage()
            page[0] += 1
            y = header(False) - 6
        cx = M + col * cell
        a, b = names(x, flats, nota)
        c.setFillColorRGB(0.1, 0.07, 0.05)
        c.setFont("DVB", 13)
        c.drawString(cx + 6, y - 14, a)
        c.setFont("DV", 7.5)
        c.setFillColorRGB(0.4, 0.35, 0.3)
        c.drawString(cx + 6, y - 23, b)
        if v:
            diagram(cx + 2, y - 34, v, 1.0)
            if v.get("capo"):
                c.setFont("DV", 6.5)
                c.setFillColorRGB(0.45, 0.4, 0.35)
                c.drawString(cx + 6, y - 112, f"forma de {v.get('shape', '')}"[:24])
    y -= 132

    # --- 2) partitura de acordes, compás a compás
    c.setFont("DVB", 12)
    c.setFillColorRGB(0.1, 0.07, 0.05)
    c.drawString(M, y, "Compás a compás")
    y -= 10
    bars = bars_of(data)
    per = 4
    left = M + 46
    bw = (PW - left - M) / per
    rh = 34
    prev_label = None
    for r0 in range(0, len(bars), per):
        if y - rh < 40:
            footer()
            c.showPage()
            page[0] += 1
            y = header(False) - 6
        row = bars[r0:r0 + per]
        c.setFont("DV", 8)
        c.setFillColorRGB(0.45, 0.4, 0.35)
        c.drawString(M, y - 14, fmt_t(row[0][0]))
        c.drawString(M, y - 25, f"c. {row[0][2]}" if row[0][2] else "inicio")
        for k, (a, b, num) in enumerate(row):
            x = left + k * bw
            c.setStrokeColorRGB(0.55, 0.5, 0.45)
            c.setLineWidth(0.8)
            c.line(x, y, x, y - rh)
            if k == len(row) - 1:
                c.line(x + bw, y, x + bw, y - rh)
            c.setStrokeColorRGB(0.85, 0.8, 0.75)
            c.setLineWidth(0.4)
            for q in range(1, meter):
                tx = x + bw * q / meter
                c.line(tx, y - rh + 3, tx, y - rh + 7)
            inbar = [s for s in segs if s["start"] < b - 0.02 and s["end"] > a + 0.02]
            for s_ in inbar:
                t0 = max(s_["start"], a)
                px = x + 6 + (bw - 12) * (t0 - a) / max(0.05, b - a)
                if s_.get("root") is None:
                    if s_["start"] >= a - 0.02:
                        c.setFillColorRGB(0.6, 0.55, 0.5)
                        c.setFont("DV", 11)
                        c.drawString(px, y - 21, "—")
                    continue
                nm = names(s_, flats, nota)[0]
                same = s_["label"] == prev_label and s_["start"] < a - 0.02
                c.setFillColorRGB(*(rgb((150, 140, 130)) if same else rgb((26, 18, 12))))
                c.setFont("DVB", 14 if not same else 12)
                c.drawString(px, y - 21, nm if not same else f"({nm})")
                prev_label = s_["label"]
        y -= rh + 6
    footer()
    c.save()
    return out_path
