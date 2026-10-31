"""Dibuja el icono de la app (diseño propio, sin imágenes de terceros).

Crea icon.png, icon.ico (Windows) y, en macOS, el AppIcon.icns del lanzador.
Uso:  python make_icon.py
"""
import math
import platform
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
BASE = Path(__file__).resolve().parent


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def radial(size, inner, outer, center=(0.5, 0.42)):
    img = Image.new("RGB", (size, size))
    px = img.load()
    cx, cy = center[0] * size, center[1] * size
    rmax = size * 0.85
    for y in range(size):
        for x in range(size):
            t = min(1.0, math.hypot(x - cx, y - cy) / rmax)
            px[x, y] = lerp(inner, outer, t ** 1.2)
    return img


def guitar(size):
    """Cuerpo de guitarra visto de frente, inclinado, con boca, roseta y cuerdas."""
    W = size * 2  # se dibuja al doble y se reduce (bordes suaves)
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = W * 0.5
    wood_dark, wood, wood_light = (122, 62, 24), (196, 120, 58), (232, 170, 96)

    # cuerpo: dos óvalos (lóbulo inferior grande, superior pequeño) y la cintura
    lower = (cx - W * 0.30, W * 0.50, cx + W * 0.30, W * 0.94)
    upper = (cx - W * 0.235, W * 0.25, cx + W * 0.235, W * 0.62)
    body = Image.new("L", (W, W), 0)
    bd = ImageDraw.Draw(body)
    bd.ellipse(lower, fill=255)
    bd.ellipse(upper, fill=255)
    bd.ellipse((cx - W * 0.215, W * 0.44, cx + W * 0.215, W * 0.74), fill=255)  # cintura suave

    # madera con degradado
    grad = Image.new("RGB", (W, W))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        gd.line((0, y, W, y), fill=lerp(wood_light, wood, min(1, y / W * 1.3)))
    edge = body.filter(ImageFilter.GaussianBlur(W * 0.03))
    shade = Image.new("RGB", (W, W), wood_dark)
    wood_img = Image.composite(grad, shade, edge.point(lambda v: min(255, int(v * 1.25))))
    layer.paste(wood_img, (0, 0), body)

    # filete del borde
    outline = body.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(9))
    layer.paste((70, 30, 10, 255), (0, 0), outline)

    # mástil con trastes
    d.rounded_rectangle((cx - W * 0.045, W * -0.05, cx + W * 0.045, W * 0.42), radius=W * 0.01, fill=(58, 30, 14))
    for i in range(1, 9):
        y = W * 0.42 - i * W * 0.052
        d.line((cx - W * 0.045, y, cx + W * 0.045, y), fill=(205, 190, 160), width=max(2, W // 400))

    # roseta y boca
    hc, hr = (cx, W * 0.535), W * 0.10
    for r, col in ((hr + W * 0.035, (40, 18, 6)), (hr + W * 0.028, (214, 168, 78)),
                   (hr + W * 0.018, (120, 40, 26)), (hr + W * 0.010, (214, 168, 78))):
        d.ellipse((hc[0] - r, hc[1] - r, hc[0] + r, hc[1] + r), fill=col)
    d.ellipse((hc[0] - hr, hc[1] - hr, hc[0] + hr, hc[1] + hr), fill=(18, 8, 4))

    # golpeador flamenco (transparente) y puente
    d.polygon([(cx + W * 0.06, W * 0.60), (cx + W * 0.22, W * 0.62), (cx + W * 0.20, W * 0.78),
               (cx + W * 0.08, W * 0.72)], fill=(255, 245, 220, 40))
    d.rounded_rectangle((cx - W * 0.13, W * 0.775, cx + W * 0.13, W * 0.815), radius=W * 0.012, fill=(52, 24, 10))

    # cuerdas
    for i in range(6):
        x = cx - W * 0.03 + i * W * 0.012
        d.line((x, W * -0.05, x, W * 0.795), fill=(240, 232, 214, 235), width=max(2, W // 512))

    layer = layer.rotate(-28, resample=Image.BICUBIC, center=(W * 0.5, W * 0.55))
    return layer.resize((size, size), Image.LANCZOS)


def draw():
    m = 100  # margen estándar de los iconos de macOS
    size = S - 2 * m
    bg = radial(size // 4, (196, 38, 34), (46, 8, 10)).resize((size, size), Image.BICUBIC)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=185, fill=255)

    art = bg.convert("RGBA")
    g = guitar(size)
    sh = Image.new("RGBA", g.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 140), (0, 0), g.split()[3])
    art.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), (10, 18))
    art.alpha_composite(g)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((m, m + 14, S - m, S - m + 14), radius=185, fill=(0, 0, 0, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
    img.paste(art, (m, m), mask)
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((m + 4, m + 4, S - m - 4, S - m - 4), radius=181,
                                           outline=(214, 168, 78, 230), width=10)
    img.alpha_composite(ring)
    return img


def main():
    img = draw()
    img.save(BASE / "icon.png")
    img.crop((80, 80, S - 80, S - 80)).save(
        BASE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if platform.system() == "Darwin":
        with tempfile.TemporaryDirectory() as d:
            iconset = Path(d) / "AppIcon.iconset"
            iconset.mkdir()
            for s in (16, 32, 128, 256, 512):
                img.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
                img.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
            out = BASE / "Guitarra Flamenca.app/Contents/Resources/AppIcon.icns"
            out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print("Icono creado")


if __name__ == "__main__":
    main()
