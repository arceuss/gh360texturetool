#!/usr/bin/env python3
"""Unpack a GH3 Xbox 360 zone pak (global, MISC, ...) to editable PNGs.

    py -3.11 unpack.py                       # interactive (prompts)
    py -3.11 unpack.py <pak-or-folder> [out] # scripted

Give a .pak.xen file OR the folder that holds it (the .pab.xen sibling is found
automatically). Auto-decompresses if compressed, then writes every texture as a
PNG under <out>/tex/ (atlas/HUD) and <out>/img/ (GUI), plus a manifest.json.
Default output is the input folder itself.

Real texture names: if a dbg.pak sits next to the source (its folder or a sibling
PAK dir) it's used automatically, and if a neversoft-script-library/gh3 tree is
found nearby the note/gem/HUD textures are named via their materials too. So PNGs
come out named (img/training_guitar.png, tex/global_gfx/sys_gem2d_yellow.png).
Pass a dbg.pak explicitly as the 4th arg, or "-" to disable dbg names. Textures
with no known name keep their 0x<checksum> filename (still valid for repack).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh3tex


def _clean(s):
    return s.strip().strip('"').strip()


def _prompt(msg, default=None):
    return _clean(input(msg)) or default


def main(argv):
    print("GH3 Xbox 360 Texture Unpacker")
    print("Auto-decompresses if needed; extracts every TEX + .img texture to PNG.\n")

    raw = _clean(argv[0]) if len(argv) >= 1 else _prompt(
        "Path to a .pak.xen (or the folder that contains it): ")
    try:
        pak = gh3tex.resolve_pak_path(raw)
    except FileNotFoundError as e:
        print(f"  {e}"); return 1

    default_out = str(pak.parent)   # by default, tex/ + img/ land in the input folder
    out = _clean(argv[1]) if len(argv) >= 2 else _prompt(f"Output folder [{default_out}]: ", default_out)

    if pak.name.lower().endswith(".pak.ps3"):
        plat = "ps3"                       # PS3 is unambiguous from the filename
    else:
        plat = _clean(argv[2]) if len(argv) >= 3 else _prompt(
            "Is this pak Xbox, PC or PS3? (xbox / pc / ps3 / Enter = auto-detect): ")
        plat = plat.lower() if plat else None
    if plat not in (None, "xbox", "pc", "ps3"):
        print("  platform must be 'xbox', 'pc', 'ps3', or blank"); return 1

    dbg = _clean(argv[3]) if len(argv) >= 4 else None   # explicit dbg.pak; "-" = bundle only; else auto

    print()
    try:
        gh3tex.unpack(pak, out, platform=plat, dbg=dbg)
    except Exception as e:
        print(f"  ERROR: {e}"); return 1
    print(f"\nDone. PNGs are in {out}\\tex and {out}\\img.")
    print(f"Xbox: edit them and run repack.py {out}.")
    print("PC: copy the ones you want into an Xbox unpack folder (same names), then repack that.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
