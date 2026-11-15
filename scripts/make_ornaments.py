"""Generate the certificate's security-print ornaments (original artwork, vector where possible).

  app/static/brand/frame.svg    guilloche border band + rules (A4 portrait, mm units)
  app/static/brand/rosette.svg  faint spirograph watermark
  app/static/brand/seal.png     gold "official seal" of the center (transparent PNG, 1200 px)

Run again after changing the brand name:  python scripts/make_ornaments.py "PROTOCOL S" "FOR TRAINING AND CONSULTANCY"
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "app/static/brand"
FONTS = ROOT / "app/static/fonts"
NAVY, GOLD = "#0E2240", "#B08D57"


def _wave(points):
    return "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in points)


def frame_svg() -> str:
    W, H, o, i = 210, 297, 8.0, 13.6       # outer and inner edge of the guilloche band
    mid, amp = (o + i) / 2, (i - o) / 2 - 0.35
    paths = []
    for k in range(10):
        ph = k * math.pi / 5
        a = amp * (1.0 if k % 2 == 0 else 0.62)
        lam = 5.2
        top = [(x, mid + a * math.sin(2 * math.pi * x / lam + ph)) for x in [o + j * 0.25 for j in range(int((W - 2 * o) / 0.25) + 1)]]
        bottom = [(x, H - mid + a * math.sin(2 * math.pi * x / lam + ph)) for x, _ in top]
        left = [(mid + a * math.sin(2 * math.pi * y / lam + ph), y) for y in [o + j * 0.25 for j in range(int((H - 2 * o) / 0.25) + 1)]]
        right = [(W - mid + a * math.sin(2 * math.pi * y / lam + ph), y) for _, y in left]
        colour = GOLD if k % 2 == 0 else NAVY
        op = 0.85 if k % 2 == 0 else 0.35
        for pts in (top, bottom, left, right):
            paths.append(f'<path d="{_wave(pts)}" fill="none" stroke="{colour}" stroke-opacity="{op}" stroke-width="0.11"/>')
    clip = f"M{o} {o} H{W - o} V{H - o} H{o} Z M{i} {i} V{H - i} H{W - i} V{i} Z"
    sq = 3.0
    corners = "".join(
        f'<rect x="{x - sq / 2:.2f}" y="{y - sq / 2:.2f}" width="{sq}" height="{sq}" fill="{NAVY}"/>'
        f'<rect x="{x - 0.9:.2f}" y="{y - 0.9:.2f}" width="1.8" height="1.8" fill="{GOLD}"/>'
        for x, y in ((mid, mid), (W - mid, mid), (mid, H - mid), (W - mid, H - mid)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">
<defs><clipPath id="band"><path d="{clip}" clip-rule="evenodd"/></clipPath></defs>
<g clip-path="url(#band)">{''.join(paths)}</g>
<rect x="{o}" y="{o}" width="{W - 2 * o}" height="{H - 2 * o}" fill="none" stroke="{NAVY}" stroke-width="0.7"/>
<rect x="{i}" y="{i}" width="{W - 2 * i}" height="{H - 2 * i}" fill="none" stroke="{NAVY}" stroke-width="0.35"/>
<rect x="{i + 1.6}" y="{i + 1.6}" width="{W - 2 * i - 3.2}" height="{H - 2 * i - 3.2}" fill="none" stroke="{GOLD}" stroke-width="0.25"/>
{corners}
</svg>"""


def rosette_svg() -> str:
    paths = []
    for R, r, d, turns, op in ((50, 13, 34, 13, 0.55), (42, 15, 24, 15, 0.45), (30, 7, 16, 7, 0.5)):
        pts = []
        steps = 5000
        for j in range(steps + 1):
            t = 2 * math.pi * turns * j / steps
            x = (R - r) * math.cos(t) + d * math.cos((R - r) / r * t)
            y = (R - r) * math.sin(t) - d * math.sin((R - r) / r * t)
            pts.append((60 + x * 0.68, 60 + y * 0.68))
        paths.append(f'<path d="{_wave(pts)}" fill="none" stroke="{NAVY}" stroke-opacity="{op}" stroke-width="0.12"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="120mm" height="120mm" viewBox="0 0 120 120">{"".join(paths)}</svg>'


def seal_png(name: str, descriptor: str, size: int = 1200) -> Image.Image:
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gold = (176, 141, 87, 255)
    navy = (14, 34, 64, 255)
    c = S / 2
    # outer serrated ring
    teeth, r1, r2 = 72, S * 0.495, S * 0.468
    pts = []
    for k in range(teeth * 2):
        a = math.pi * k / teeth
        r = r1 if k % 2 == 0 else r2
        pts.append((c + r * math.cos(a), c + r * math.sin(a)))
    d.polygon(pts, fill=gold)
    d.ellipse([c - S * 0.445, c - S * 0.445, c + S * 0.445, c + S * 0.445], fill=(255, 255, 255, 255))
    d.ellipse([c - S * 0.43, c - S * 0.43, c + S * 0.43, c + S * 0.43], outline=gold, width=int(S * 0.008))
    d.ellipse([c - S * 0.30, c - S * 0.30, c + S * 0.30, c + S * 0.30], outline=gold, width=int(S * 0.006))
    # text around the ring
    text = f"{name}  •  {descriptor}  •  "
    font = ImageFont.truetype(str(FONTS / "IBMPlexSans-SemiBold.ttf"), int(S * 0.058))
    radius = S * 0.365
    total = sum(font.getlength(ch) for ch in text) * 1.06
    angle = -math.pi / 2 - (total / radius) / 2
    for ch in text:
        w = font.getlength(ch) * 1.06
        a = angle + (w / 2) / radius
        glyph = Image.new("RGBA", (int(S * 0.12), int(S * 0.12)), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glyph)
        gd.text((glyph.width / 2, glyph.height / 2), ch, font=font, fill=navy, anchor="mm")
        glyph = glyph.rotate(-math.degrees(a + math.pi / 2), resample=Image.BICUBIC)
        x, y = c + radius * math.cos(a), c + radius * math.sin(a)
        img.alpha_composite(glyph, (int(x - glyph.width / 2), int(y - glyph.height / 2)))
        angle += w / radius
    # monogram in the centre (same geometry as logo-mark.svg)
    u = S * 0.0042
    ox = oy = c - 50 * u
    for x, y, w, h, col in ((12, 12, 60, 12, navy), (76, 12, 12, 12, gold), (12, 28, 12, 12, navy),
                            (12, 44, 76, 12, navy), (76, 60, 12, 12, navy), (12, 76, 76, 12, navy)):
        d.rectangle([ox + x * u, oy + y * u, ox + (x + w) * u, oy + (y + h) * u], fill=col)
    return img


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "PROTOCOL S"
    desc = sys.argv[2] if len(sys.argv) > 2 else "FOR TRAINING AND CONSULTANCY"
    (BRAND / "frame.svg").write_text(frame_svg())
    (BRAND / "rosette.svg").write_text(rosette_svg())
    seal_png(name, desc).save(BRAND / "seal.png")
    print("written:", *(p.name for p in BRAND.iterdir()))
