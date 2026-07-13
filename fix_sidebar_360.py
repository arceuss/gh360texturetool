#!/usr/bin/env python3
"""Make a GH3 PC sidebar texture render 1:1 on Xbox 360.

    py fix_sidebar_360.py <sidebar.png> [-o out.png]     # warp one PNG
    py fix_sidebar_360.py --unpack <folder>              # patch tex/global_gfx/sys_sidebar2D.png
                                                         # in an unpack folder, then just repack

The 360 engine draws the highway sidebar sprite on a different quad than GH3 PC
(lower, larger, wider), so the same texture renders ~20px inward and ~20% thicker
on console. This script resamples the texture through a calibrated warp so the 360
render lands pixel-for-pixel on the PC render.

The warp field (sidebar_360_warp.npz, beside this script) was measured empirically:
a calibration texture (hue = column, white lines = rows) was screenshotted on real
hardware and GH3 PC at 720p, marker-line fits gave dense texture->screen maps for
both engines (left/right sprites agreed to ~1.5 texels), and the warp is their
composition F_pc^-1(F_360). Validated: warped rail lands on the measured PC rail
curve with rms 0.17px (max 0.46px); calibration bias vs reality +0.46px.

Scope: the sidebar sprite (sys_sidebar2D) on a 400x512 canvas (the FastGH3 theme
convention; rainbow/ggamer/WoR all use it). Other sizes are resampled to 400x512
first. 720p geometry; other output resolutions scale with the framebuffer.
"""
import argparse, sys
from pathlib import Path
import numpy as np
from PIL import Image

TEXW, TEXH = 400, 512


def load_field():
    p = Path(__file__).resolve().parent / "sidebar_360_warp.npz"
    if not p.exists():
        sys.exit(f"missing warp field: {p}")
    wf = np.load(p)
    return wf["wc"].astype(float), wf["wr"].astype(float)


def warp_sidebar(img: Image.Image) -> Image.Image:
    if img.size != (TEXW, TEXH):
        print(f"  note: {img.size[0]}x{img.size[1]} input resampled to {TEXW}x{TEXH} "
              f"(the warp is calibrated for the 400x512 sidebar canvas)")
        img = img.resize((TEXW, TEXH), Image.LANCZOS)
    src = np.asarray(img.convert("RGBA")).astype(float)
    WC, WR = load_field()
    # alpha-premultiplied bilinear resample (no halo bleed at soft edges)
    pm = src.copy(); pm[..., :3] *= pm[..., 3:4] / 255.0
    x0 = np.floor(WC).astype(int); y0 = np.floor(WR).astype(int)
    fx = WC - x0; fy = WR - y0
    valid = (WC >= 0) & (WC <= TEXW - 1) & (WR >= 0) & (WR <= TEXH - 1)
    x0c = np.clip(x0, 0, TEXW - 2); y0c = np.clip(y0, 0, TEXH - 2)
    out = np.zeros_like(pm)
    for dy in (0, 1):
        for dx in (0, 1):
            w = (fx if dx else 1 - fx) * (fy if dy else 1 - fy)
            out += w[..., None] * pm[y0c + dy, x0c + dx]
    out[~valid] = 0
    a = np.maximum(out[..., 3:4], 1e-6)
    out[..., :3] = out[..., :3] / a * 255.0
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def main(argv):
    ap = argparse.ArgumentParser(description="warp a GH3 PC sidebar texture for 1:1 Xbox 360 rendering")
    ap.add_argument("png", nargs="?", help="sidebar PNG (usually sys_sidebar2D.png from a PC unpack)")
    ap.add_argument("-o", "--out", help="output PNG (default: <name>_360fix.png)")
    ap.add_argument("--unpack", help="unpack folder: patch tex/global_gfx/sys_sidebar2D.png in place")
    a = ap.parse_args(argv)

    if a.unpack:
        p = Path(a.unpack) / "tex" / "global_gfx" / "sys_sidebar2D.png"
        if not p.exists():
            sys.exit(f"not found: {p}")
        img = warp_sidebar(Image.open(p))
        img.save(p)
        print(f"patched in place: {p}\nnow repack the folder (py repack.py {a.unpack})")
        return 0

    if not a.png:
        ap.print_help(); return 1
    src = Path(a.png)
    out = Path(a.out) if a.out else src.with_name(src.stem + "_360fix.png")
    warp_sidebar(Image.open(src)).save(out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
