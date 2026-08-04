"""Generate the brand images: a stylized front view of the Honeywell Tuxedo
Touch wall-mount touchscreen keypad.

Renders at 4x and downsamples (LANCZOS) for crisp edges. Needs only Pillow.

    python generate.py                 # contact sheet into ./preview for review
    python generate.py --final grid    # write the PNGs into the integration's
                                       # brand/ dir (served by HA 2026.3+'s
                                       # Brands Proxy API)

Sizes follow the Home Assistant brand rules: icon 256/512 square; logo
shortest side 256 (512 for @2x), trimmed; dark_ variants carry white text.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "preview")
os.makedirs(OUT, exist_ok=True)

# ---------------------------------------------------------------- palette
BODY_TOP = (236, 238, 241)     # silver housing, lit from above
BODY_BOT = (196, 201, 208)
BODY_EDGE = (150, 156, 165)
GLASS = (16, 19, 24)           # glossy black glass face
GLASS_EDGE = (5, 6, 8)
SCREEN_TOP = (36, 74, 134)     # Tuxedo home-screen blue
SCREEN_BOT = (10, 28, 62)
BANNER = (58, 176, 92)         # "Ready To Arm" green
BANNER_DARK = (38, 136, 66)
WHITE = (255, 255, 255)
APP_COLORS = [
    (52, 168, 186),   # teal   - security
    (232, 163, 61),   # amber  - lighting
    (86, 116, 214),   # indigo - thermostat
    (98, 179, 104),   # green  - automation
    (214, 100, 70),   # coral  - camera
    (128, 142, 164),  # slate  - settings
]


def vgrad(size, top, bot):
    """Vertical gradient image."""
    w, h = size
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line(
            [(0, y), (w, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)),
        )
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return m


def paste_rounded_grad(base, box, radius, top, bot):
    w, h = box[2] - box[0], box[3] - box[1]
    grad = vgrad((w, h), top, bot)
    base.paste(grad, (box[0], box[1]), rounded_mask((w, h), radius))


def draw_device(canvas_w, canvas_h, variant="grid"):
    """Draw the device filling the canvas (device aspect ~1.55:1), RGBA."""
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    W, H = canvas_w, canvas_h
    # housing
    paste_rounded_grad(img, (0, 0, W, H), int(H * 0.10), BODY_TOP, BODY_BOT)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], int(H * 0.10), outline=BODY_EDGE, width=max(2, H // 256))

    # glass face
    gx0, gy0 = int(W * 0.035), int(H * 0.055)
    gx1, gy1 = W - gx0, H - gy0
    d.rounded_rectangle([gx0, gy0, gx1, gy1], int(H * 0.065), fill=GLASS, outline=GLASS_EDGE,
                        width=max(2, H // 300))

    # screen
    sx0 = gx0 + int(W * 0.045)
    sy0 = gy0 + int(H * 0.075)
    sx1 = gx1 - int(W * 0.045)
    sy1 = gy1 - int(H * 0.11)
    paste_rounded_grad(img, (sx0, sy0, sx1, sy1), int(H * 0.02), SCREEN_TOP, SCREEN_BOT)
    d = ImageDraw.Draw(img)

    sw, sh = sx1 - sx0, sy1 - sy0

    # green status banner with a check mark
    bx0, by0 = sx0 + int(sw * 0.03), sy0 + int(sh * 0.05)
    bx1, by1 = sx1 - int(sw * 0.03), sy0 + int(sh * 0.24)
    d.rounded_rectangle([bx0, by0, bx1, by1], int(sh * 0.045), fill=BANNER, outline=BANNER_DARK,
                        width=max(1, H // 400))
    ch = by1 - by0
    ccx, ccy = (bx0 + bx1) // 2, (by0 + by1) // 2
    lw = max(3, ch // 5)
    d.line([(ccx - ch * 0.55, ccy - ch * 0.02), (ccx - ch * 0.18, ccy + ch * 0.28),
            (ccx + ch * 0.55, ccy - ch * 0.28)], fill=WHITE, width=lw, joint="curve")

    if variant == "grid":
        # 2x3 round app icons, like the Tuxedo home screen
        rows, cols = 2, 3
        area_y0 = by1 + int(sh * 0.09)
        area_y1 = sy1 - int(sh * 0.06)
        cell_w = sw // cols
        cell_h = (area_y1 - area_y0) // rows
        r = int(min(cell_w, cell_h) * 0.34)
        for i in range(rows * cols):
            row, col = divmod(i, cols)
            cx = sx0 + cell_w * col + cell_w // 2
            cy = area_y0 + cell_h * row + cell_h // 2
            color = APP_COLORS[i]
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color,
                      outline=tuple(max(0, c - 40) for c in color), width=max(1, H // 400))
            # glossy highlight on each icon
            hi = Image.new("RGBA", img.size, (0, 0, 0, 0))
            hd = ImageDraw.Draw(hi)
            hd.ellipse([cx - int(r * 0.52), cy - int(r * 0.74),
                        cx + int(r * 0.52), cy - int(r * 0.22)], fill=(255, 255, 255, 36))
            img.alpha_composite(hi)
            d = ImageDraw.Draw(img)
    else:
        # "shield" variant: one big shield glyph under the banner
        area_y0 = by1 + int(sh * 0.10)
        area_y1 = sy1 - int(sh * 0.08)
        cy = (area_y0 + area_y1) // 2
        cx = (sx0 + sx1) // 2
        shw = int((area_y1 - area_y0) * 0.75)
        shh = area_y1 - area_y0
        top = area_y0
        pts = [
            (cx - shw, top + int(shh * 0.10)),
            (cx, top),
            (cx + shw, top + int(shh * 0.10)),
            (cx + shw, top + int(shh * 0.55)),
            (cx, top + shh),
            (cx - shw, top + int(shh * 0.55)),
        ]
        d.polygon(pts, fill=(238, 242, 247), outline=(180, 190, 200))
        lw = max(4, shh // 10)
        d.line([(cx - shw * 0.45, cy - shh * 0.08), (cx - shw * 0.12, cy + shh * 0.16),
                (cx + shw * 0.5, cy - shh * 0.22)], fill=BANNER, width=lw, joint="curve")

    # speaker slot on the lower glass bezel
    slot_w = int(W * 0.14)
    slot_y = (sy1 + gy1) // 2
    d.rounded_rectangle([W // 2 - slot_w // 2, slot_y - max(2, H // 220),
                         W // 2 + slot_w // 2, slot_y + max(2, H // 220)],
                        max(2, H // 220), fill=(70, 76, 84))

    # diagonal glass sheen, clipped to the glass
    sheen = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    sd.polygon([(gx0, gy0), (int(W * 0.52), gy0), (int(W * 0.30), gy1), (gx0, gy1)],
               fill=(255, 255, 255, 14))
    clip = Image.new("L", img.size, 0)
    ImageDraw.Draw(clip).rounded_rectangle([gx0, gy0, gx1, gy1], int(H * 0.065), fill=255)
    sheen.putalpha(Image.composite(sheen.getchannel("A"), Image.new("L", img.size, 0), clip))
    img.alpha_composite(sheen)
    return img


def make_icon(variant, master=2048):
    """Square icon: device centered, transparent background."""
    canvas = Image.new("RGBA", (master, master), (0, 0, 0, 0))
    dev_w = int(master * 0.98)
    dev_h = int(dev_w / 1.55)
    dev = draw_device(dev_w, dev_h, variant)
    canvas.alpha_composite(dev, ((master - dev_w) // 2, (master - dev_h) // 2))
    return canvas


def make_logo(variant, dark=False, master_h=1024):
    """Landscape logo: device left, 'Tuxedo Touch' wordmark right."""
    dev_h = master_h
    dev_w = int(dev_h * 1.55)
    dev = draw_device(dev_w, dev_h, variant)

    font_path = next(
        p
        for p in (
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        )
        if os.path.exists(p)
    )
    font = ImageFont.truetype(font_path, int(master_h * 0.34))
    text = "Tuxedo Touch"
    color = (244, 246, 248) if dark else (27, 31, 36)
    tmp = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    gap = int(master_h * 0.12)
    W = dev_w + gap + tw + int(master_h * 0.04)
    canvas = Image.new("RGBA", (W, master_h), (0, 0, 0, 0))
    canvas.alpha_composite(dev, (0, 0))
    d = ImageDraw.Draw(canvas)
    d.text((dev_w + gap - bbox[0], (master_h - th) // 2 - bbox[1]), text, font=font, fill=color)
    return canvas


def trim(img, pad=0):
    bbox = img.getchannel("A").getbbox()
    img = img.crop(bbox)
    if pad:
        out = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        out.alpha_composite(img, (pad, pad))
        img = out
    return img


def save_scaled(img, path, target_short):
    short = min(img.size)
    scale = target_short / short
    out = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    out.save(path, optimize=True)
    return out.size


def contact_sheet():
    tiles = []
    for variant in ("grid", "shield"):
        icon = make_icon(variant).resize((256, 256), Image.LANCZOS)
        logo = trim(make_logo(variant))
        logo = logo.resize((round(logo.width * 256 / logo.height), 256), Image.LANCZOS)
        dark_logo = trim(make_logo(variant, dark=True))
        dark_logo = dark_logo.resize((round(dark_logo.width * 256 / dark_logo.height), 256), Image.LANCZOS)
        tiles.append((variant, icon, logo, dark_logo))

    pad = 24
    col_w = max(t[1].width + t[2].width + 3 * pad for t in tiles)
    row_h = 256 + 2 * pad
    sheet = Image.new("RGB", (col_w + pad, row_h * len(tiles) * 2), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    y = 0
    for variant, icon, logo, dark_logo in tiles:
        # white background row: icon + light logo
        sheet.paste(icon, (pad, y + pad), icon)
        sheet.paste(logo, (pad * 2 + icon.width, y + pad), logo)
        y += row_h
        # dark background row: icon + dark logo
        d.rectangle([0, y, sheet.width, y + row_h], fill=(24, 27, 33))
        sheet.paste(icon, (pad, y + pad), icon)
        sheet.paste(dark_logo, (pad * 2 + icon.width, y + pad), dark_logo)
        y += row_h
    sheet.save(OUT + r"\contact_sheet.png", optimize=True)
    print("sheet:", OUT + r"\contact_sheet.png")


if __name__ == "__main__":
    if "--final" in sys.argv:
        variant = sys.argv[sys.argv.index("--final") + 1]
        dest = os.path.normpath(
            os.path.join(HERE, "..", "custom_components", "tuxedo_touch", "brand")
        )
        os.makedirs(dest, exist_ok=True)
        icon = make_icon(variant)
        print("icon@2x:", save_scaled(icon, dest + r"\icon@2x.png", 512))
        print("icon:", save_scaled(icon, dest + r"\icon.png", 256))
        logo = trim(make_logo(variant))
        print("logo@2x:", save_scaled(logo, dest + r"\logo@2x.png", 512))
        print("logo:", save_scaled(logo, dest + r"\logo.png", 256))
        dark = trim(make_logo(variant, dark=True))
        print("dark_logo@2x:", save_scaled(dark, dest + r"\dark_logo@2x.png", 512))
        print("dark_logo:", save_scaled(dark, dest + r"\dark_logo.png", 256))
    else:
        contact_sheet()
