#!/usr/bin/env python3
"""Repack edited PNGs from an unpack folder back into a GH3 Xbox 360 pak.

    py -3.11 repack.py                          # interactive (prompts)
    py -3.11 repack.py <unpack_folder> [out.pak.xen]

Point it at the folder unpack.py made (the one with manifest.json + tex/ + img/).
It re-decompresses the original pak, re-encodes ONLY the PNGs whose pixels changed
since unpack, and writes an uncompressed, install-ready pak (default
<name>_new.pak.xen in that folder). Untouched textures stay byte-identical.
Install: global -> game DATA\\ZONES, MISC -> DATA\\MISC (no recompression).
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
    print("GH3 Xbox 360 Texture Repacker")
    print("Re-encodes only the PNGs you changed; output is install-ready (uncompressed).\n")

    work = _clean(argv[0]) if len(argv) >= 1 else _prompt("Path to the unpack folder (has manifest.json): ")
    if not work or not (Path(work) / "manifest.json").exists():
        print("  No manifest.json there - point at a folder made by unpack.py."); return 1
    out_pak = _clean(argv[1]) if len(argv) >= 2 else None

    print()
    try:
        n = gh3tex.repack(work, out_pak)
    except Exception as e:
        print(f"  ERROR: {e}"); return 1
    if n:
        print("\nDone. Copy the *_new.pak.xen + *_new.pab.xen into the game's DATA folder")
        print("(global -> DATA\\ZONES, MISC -> DATA\\MISC). No recompression needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
