#!/usr/bin/env python3
"""Generate the synthetic fixtures for the clef-flash vision demo.

All images are drawn from primitives — no external assets, no real data.
Requires Pillow (generation only; the demo runner is stdlib-only).

Run once from the repo root:

    python3 harness/make_vision_fixtures.py

Writes PNGs to ``assets/vision_demo/``.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "vision_demo"

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{FONT_DIR}/{name}", size)


REG = "DejaVuSans.ttf"
BOLD = "DejaVuSans-Bold.ttf"
MONO = "DejaVuSansMono.ttf"

INK = (15, 23, 42)
MUTED = (100, 116, 139)
PANEL = (31, 41, 55)
BG = (248, 250, 252)
WHITE = (255, 255, 255)
RED = (220, 38, 38)
GREEN = (22, 163, 74)
BLUE = (37, 99, 235)


def draw_chrome(d: ImageDraw.ImageDraw, title: str) -> None:
    """Shared app chrome: top bar + sidebar, for the two UI screenshots."""
    d.rectangle((0, 0, 800, 56), fill=(15, 23, 42))
    d.text((22, 15), title, font=font(BOLD, 22), fill=WHITE)
    d.rounded_rectangle((640, 14, 780, 42), 8, fill=(30, 41, 59))
    d.text((656, 20), "admin@acme.io", font=font(REG, 14), fill=(148, 163, 184))
    d.rectangle((0, 56, 180, 520), fill=PANEL)
    for i, item in enumerate(["Overview", "Orders", "Customers", "Settings"]):
        d.text((24, 96 + i * 44), item, font=font(REG, 18),
               fill=WHITE if i == 1 else (148, 163, 184))


def ui_error() -> None:
    img = Image.new("RGB", (800, 520), BG)
    d = ImageDraw.Draw(img)
    draw_chrome(d, "Acme Ops Console")
    d.text((210, 84), "Orders", font=font(BOLD, 20), fill=INK)
    rows = [("#10231", "shipped", "€120.00"), ("#10232", "shipped", "€84.00"),
            ("#10233", "pending", "€499.00"), ("#10234", "shipped", "€61.50"),
            ("#10235", "shipped", "€230.00")]
    for i, (oid, st, amt) in enumerate(rows):
        y = 120 + i * 38
        d.rectangle((205, y, 775, y + 30), fill=(226, 232, 240) if i % 2 == 0 else WHITE)
        d.text((218, y + 5), oid, font=font(MONO, 16), fill=MUTED)
        d.text((380, y + 5), st, font=font(REG, 16), fill=MUTED)
        d.text((640, y + 5), amt, font=font(MONO, 16), fill=MUTED)

    # Modal with shadow.
    d.rounded_rectangle((232, 156, 592, 396), 14, fill=(203, 213, 225))
    d.rounded_rectangle((220, 144, 580, 384), 14, fill=WHITE)
    d.ellipse((368, 168, 432, 232), fill=RED)
    d.text((395, 186), "!", font=font(BOLD, 34), fill=WHITE)
    d.text((292, 252), "Checkout failed", font=font(BOLD, 24), fill=INK)
    d.text((306, 290), "HTTP 502 Bad Gateway", font=font(REG, 19), fill=(51, 65, 85))
    d.text((270, 318), "Orders are blocked for all customers.",
           font=font(REG, 15), fill=MUTED)
    d.rounded_rectangle((336, 340, 464, 374), 8, fill=RED)
    d.text((374, 348), "Retry", font=font(BOLD, 18), fill=WHITE)
    img.save(OUT / "ui-error.png")


def ui_checkout() -> None:
    img = Image.new("RGB", (800, 520), BG)
    d = ImageDraw.Draw(img)
    draw_chrome(d, "Acme Store")
    d.text((210, 84), "Checkout", font=font(BOLD, 20), fill=INK)
    d.rounded_rectangle((232, 130, 592, 442), 14, fill=(203, 213, 225))
    d.rounded_rectangle((220, 118, 580, 430), 14, fill=WHITE)
    d.text((244, 140), "Order summary", font=font(BOLD, 20), fill=INK)
    lines = [("Widget Pro × 1", "€460.00"), ("Shipping", "€39.00")]
    for i, (label, amt) in enumerate(lines):
        y = 190 + i * 36
        d.text((244, y), label, font=font(REG, 17), fill=(51, 65, 85))
        d.text((500, y), amt, font=font(MONO, 17), fill=(51, 65, 85))
    d.line((244, 278, 556, 278), fill=(203, 213, 225), width=1)
    d.text((244, 292), "Total", font=font(BOLD, 19), fill=INK)
    d.text((498, 292), "€499.00", font=font(BOLD, 19), fill=INK)
    d.rounded_rectangle((276, 348, 524, 400), 10, fill=GREEN)
    d.text((330, 362), "Pay €499.00", font=font(BOLD, 22), fill=WHITE)
    img.save(OUT / "ui-checkout.png")


def _chart(filename: str, values: list[int]) -> None:
    img = Image.new("RGB", (800, 460), WHITE)
    d = ImageDraw.Draw(img)
    d.text((110, 28), "API latency p95 (ms) — last hour", font=font(BOLD, 20), fill=INK)

    x0, x1, y0, y1 = 110, 750, 80, 380  # plot area; y maps 0..500
    def ypix(v: float) -> float:
        return y1 - (v / 500.0) * (y1 - y0)

    for tick in range(0, 501, 100):
        y = ypix(tick)
        d.line((x0, y, x1, y), fill=(241, 245, 249), width=1)
        d.text((72, y - 8), str(tick), font=font(REG, 14), fill=MUTED)
    d.line((x0, y0, x0, y1), fill=(148, 163, 184), width=2)
    d.line((x0, y1, x1, y1), fill=(148, 163, 184), width=2)

    # Dashed threshold at 300 ms.
    ty = ypix(300)
    x = x0
    while x < x1:
        d.line((x, ty, min(x + 14, x1), ty), fill=RED, width=2)
        x += 26
    d.text((560, ty - 26), "threshold 300 ms", font=font(REG, 15), fill=RED)

    n = len(values)
    pts = [(x0 + i * (x1 - x0) / (n - 1), ypix(v)) for i, v in enumerate(values)]
    d.line(pts, fill=BLUE, width=3, joint="curve")
    for px, py in pts:
        d.ellipse((px - 4, py - 4, px + 4, py + 4), fill=BLUE)
    d.text((x0, 404), "-60m", font=font(REG, 14), fill=MUTED)
    d.text((x1 - 30, 404), "now", font=font(REG, 14), fill=MUTED)
    img.save(OUT / filename)


def receipt() -> None:
    paper = Image.new("RGB", (500, 720), WHITE)
    d = ImageDraw.Draw(paper)
    lines = [
        ("SUPERMARKT BV", BOLD, 22),
        ("Amsterdam · 020-1234567", REG, 14),
        ("", REG, 10),
        ("2026-10-06  14:32  REG 04", REG, 16),
        ("", REG, 10),
        ("OAT MILK 1L        2.49", MONO, 17),
        ("SOURDOUGH          3.79", MONO, 17),
        ("TOMATOES 500G      1.89", MONO, 17),
        ("COFFEE BEANS 1KG  12.99", MONO, 17),
        ("CHEDDAR 400G       6.49", MONO, 17),
        ("BANANAS 1KG        2.22", MONO, 17),
        ("SPARKLING WATER 6  4.99", MONO, 17),
        ("OLIVE OIL 500ML    8.01", MONO, 17),
        ("----------------------", MONO, 17),
        ("TOTAL             42.87", MONO, 19),
        ("CARD **** 4242", MONO, 17),
        ("----------------------", MONO, 17),
        ("THANK YOU", REG, 16),
    ]
    y = 36
    for text, f, size in lines:
        d.text((36, y), text, font=font(f, size), fill=(17, 17, 17))
        y += size + 14
    # Barcode-ish block.
    rng = random.Random(20261006)
    x = 60
    while x < 440:
        w = rng.choice([2, 3, 5])
        d.rectangle((x, 620, x + w, 690), fill=(17, 17, 17))
        x += w + rng.choice([2, 4, 6])

    paper = paper.rotate(-1.6, expand=True, resample=Image.BICUBIC, fillcolor=WHITE)
    img = Image.new("RGB", (620, 800), (229, 231, 235))
    img.paste(paper, ((620 - paper.width) // 2, (800 - paper.height) // 2))
    img.save(OUT / "receipt.png")


def photo_blur() -> None:
    img = Image.new("RGB", (800, 520), WHITE)
    d = ImageDraw.Draw(img)
    for y in range(0, 520):
        if y < 330:  # sky gradient
            t = y / 330
            c = tuple(int(a + (b - a) * t) for a, b in
                      zip((125, 190, 240), (224, 242, 254)))
        else:  # field gradient
            t = (y - 330) / 190
            c = tuple(int(a + (b - a) * t) for a, b in
                      zip((101, 163, 13), (77, 124, 15)))
        d.line((0, y, 800, y), fill=c)
    d.ellipse((574, 44, 666, 136), fill=(253, 224, 71))       # sun
    d.ellipse((586, 56, 654, 124), fill=(250, 204, 21))
    d.rectangle((252, 310, 266, 400), fill=(120, 53, 15))     # trunk
    d.ellipse((204, 224, 316, 336), fill=(22, 101, 52))       # canopy
    d.ellipse((232, 250, 288, 306), fill=(34, 197, 94))
    img = img.filter(ImageFilter.GaussianBlur(9))
    img.save(OUT / "photo-blur.png")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ui_error()
    ui_checkout()
    _chart("dashboard-above.png", [120, 150, 140, 180, 210, 260, 300, 350, 410])
    _chart("dashboard-below.png", [150, 130, 160, 140, 170, 150, 160, 170, 180])
    receipt()
    photo_blur()
    for p in sorted(OUT.glob("*.png")):
        print(f"{p.name:24s} {p.stat().st_size / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
