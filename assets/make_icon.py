"""Generate the Agent Terminal Dashboard app icon (.ico + .png).

Design: a gold terminal prompt ">_" on a dark warm rounded-square,
matching the dashboard's palette (accent #e3b363, bg #0f0e0c/#1f1d1a).
Rendered at 4x supersample then downscaled for clean anti-aliasing.
"""
from PIL import Image, ImageDraw
import os, sys

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "."

# Palette
BG_TOP    = (31, 29, 26)    # #1f1d1a
BG_BOT    = (12, 11, 9)     # #0c0b09  (slightly darker than #0f0e0c for depth)
GOLD      = (227, 179, 99)  # #e3b363
GOLD_DK   = (184, 125, 16)  # #b87d10
BORDER    = (227, 179, 99)

S = 4              # supersample factor
SIZE = 256         # final master size
W = SIZE * S       # working canvas size

img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- rounded-square background with vertical gradient ---
radius = int(W * 0.225)
# build gradient on its own layer, then mask to rounded rect
grad = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gp = grad.load()
for y in range(W):
    t = y / (W - 1)
    r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
    g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
    b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
    for x in range(W):
        gp[x, y] = (r, g, b, 255)

mask = Image.new("L", (W, W), 0)
md = ImageDraw.Draw(mask)
inset = int(W * 0.02)
md.rounded_rectangle([inset, inset, W - inset, W - inset], radius=radius, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)

# --- subtle gold border ---
bw = max(2, int(W * 0.012))
d.rounded_rectangle(
    [inset, inset, W - inset, W - inset],
    radius=radius, outline=BORDER + (90,), width=bw,
)

# --- terminal prompt ">" chevron ---
# geometry centered-left
stroke = int(W * 0.085)            # line thickness
cx = int(W * 0.30)                 # apex x of the ">"
top = int(W * 0.34)
bot = int(W * 0.66)
mid = (top + bot) // 2
left = int(W * 0.20)

def thick_line(p1, p2, width, fill):
    d.line([p1, p2], fill=fill, width=width)
    r = width // 2
    for (px, py) in (p1, p2):
        d.ellipse([px - r, py - r, px + r, py + r], fill=fill)

# the ">" = two segments meeting at the apex on the right
apex = (cx, mid)
thick_line((left, top), apex, stroke, GOLD)
thick_line((left, bot), apex, stroke, GOLD)

# --- cursor underscore bar ---
ux0 = int(W * 0.44)
ux1 = int(W * 0.74)
uy0 = int(W * 0.605)
uy1 = uy0 + stroke
d.rounded_rectangle([ux0, uy0, ux1, uy1], radius=stroke // 2, fill=GOLD)

# downscale to master size (anti-alias)
master = img.resize((SIZE, SIZE), Image.LANCZOS)

png_path = os.path.join(OUT_DIR, "app-icon.png")
ico_path = os.path.join(OUT_DIR, "app-icon.ico")
master.save(png_path)

# multi-resolution .ico
sizes = [16, 24, 32, 48, 64, 128, 256]
master.save(ico_path, format="ICO", sizes=[(s, s) for s in sizes])
print("wrote", png_path)
print("wrote", ico_path)
