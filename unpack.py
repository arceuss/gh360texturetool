#!/usr/bin/env python3
"""Unpack a GH3 Xbox 360 zone pak (global, MISC, ...) to editable PNGs.

    py -3.11 unpack.py                       # interactive (prompts)
    py -3.11 unpack.py <pak-or-folder> [out] # scripted

Give a .pak.xen file OR the folder that holds it (the .pab.xen sibling is found
automatically). Auto-decompresses if compressed, then writes every texture as a
PNG under <out>/tex/ (atlas/HUD) and <out>/img/ (GUI), plus a manifest.json.
Default output is the input folder itself.
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

    plat = _clean(argv[2]) if len(argv) >= 3 else _prompt(
        "Is this pak Xbox or PC? (xbox / pc / Enter = auto-detect): ")
    plat = plat.lower() if plat else None
    if plat not in (None, "xbox", "pc"):
        print("  platform must be 'xbox', 'pc', or blank"); return 1

    print()
    try:
        gh3tex.unpack(pak, out, platform=plat)
    except Exception as e:
        print(f"  ERROR: {e}"); return 1
    print(f"\nDone. PNGs are in {out}\\tex and {out}\\img.")
    print(f"Xbox: edit them and run repack.py {out}.")
    print("PC: copy the ones you want into an Xbox unpack folder (same names), then repack that.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
