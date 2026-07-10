#!/usr/bin/env python3
"""Bake a shared checksum->name map for GH3 textures, so unpack.py can name PNGs
without the user holding any dbg.pak or the script library.

Sources (developer-side only; the produced JSON is what ships):
  - each platform's dbg.pak  -> container/file names (keyed by the platform path hash)
  - the SCN material bridge   -> individual note/gem/HUD texture names (shared hashes)

Only checksums that are actually textures (container full_names + tex-record checksums
across the reference paks) are kept, so texture_names.json stays small. Console record
checksums are shared across platforms; container checksums differ per platform but never
collide, so one flat map serves all three.

    py -3.11 build_texture_names.py            # uses the default reference roots below
    py -3.11 build_texture_names.py --out texture_names.json
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gh3tex
from tools.x360pak._consult_single import SplitPak, XboxTex

TEX, IMG, TVX, IMV = 0x8BFA5E8E, 0xDAD5E950, 0xEA151F1C, 0xB065C9A2
CONTAINER_TYPES = (TEX, IMG, TVX, IMV)

# (platform, data root to scan for paks, dbg.pak path)
DEFAULT_SOURCES = [
    ("ps3", r"F:/PCtoConsole/Originals/PS3/DATA", r"F:/PCtoConsole/Originals/PS3/DATA/PAK/DBG.PAK.PS3"),
    ("xbox", r"F:/PCtoConsole/Originals/360/DATA/COMPRESSED", r"F:/PCtoConsole/Originals/360/DATA/COMPRESSED/PAK/dbg.pak.xen"),
    ("pc", r"F:/Games/Guitar Hero III/DATA", r"F:/Games/Guitar Hero III/DATA/PAK/dbg.pak.xen"),
]


def iter_ps3_paks(root):
    for p in Path(root).rglob("*.PAK.PS3"):
        u = p.name.upper()
        if "VRAM" in u or "DBG" in u:
            continue
        try:
            yield gh3tex.Ps3Pak(p)
        except Exception:
            continue


def iter_xbox_paks(root):
    for p in Path(root).rglob("*.pak.xen"):
        if ".pab." in p.name.lower() or "dbg" in p.name.lower():
            continue
        pab = p.with_name(p.name.replace(".pak.", ".pab."))
        try:
            data = gh3tex.decompress_pak_data(p.read_bytes())
            pabd = gh3tex.decompress_pak_data(pab.read_bytes()) if pab.exists() else b""
            yield SplitPak.from_bytes(data, pabd, pak_path=p, pab_path=pab)
        except Exception:
            continue


import re
TEX_EXT = re.compile(r'\.(tex|img|tvx|imv)(\.(ps3|xen))?$', re.I)


def collect(platform, root, dbg_path, tokq, names, stats):
    # 1. container/file names: every dbg entry whose path is a texture file
    dbg = gh3tex.load_dbg_names(dbg_path) if Path(dbg_path).exists() else {}
    for cs, name in dbg.items():
        base = re.split(r'[\\/]', name.strip())[-1]
        if TEX_EXT.search(base) and cs not in names:
            names[cs] = name; stats["dbg"] += 1
    # 2. read every pak that parses: resolve each texture ENTRY's full_name via dbg
    #    (captures bare img names like 'training_guitar' that have no extension) and
    #    run the SCN material bridge for individual note/gem/HUD texture names.
    is_ps3 = platform == "ps3"
    for pak in (iter_ps3_paks(root) if is_ps3 else iter_xbox_paks(root)):
        tex_cs, scn_blobs = set(), []
        for e in pak.entries:
            t = getattr(e, "type", None)
            if t in CONTAINER_TYPES and e.full_name in dbg and e.full_name not in names:
                names[e.full_name] = dbg[e.full_name]; stats["dbg"] += 1
            if t == TEX:
                data = pak.entry_data(e) if is_ps3 else e.data
                try:
                    if is_ps3:
                        tex_cs.update(cs for cs, *_ in gh3tex.ps3_tex_records(data))
                    else:
                        tex_cs.update(r.checksum for r in XboxTex.parse(data).records)
                except Exception:
                    pass
            elif t == gh3tex.SCN_TYPE:
                scn_blobs.append(pak.entry_data(e) if is_ps3 else e.data)
        for k, v in gh3tex.material_texture_names(scn_blobs, tex_cs, tokq,
                                                  big_endian=(platform != "pc")).items():
            if k not in names:
                names[k] = v; stats["material"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "texture_names.json")
    ap.add_argument("--scripts", type=Path, default=None)
    args = ap.parse_args()

    script_root = args.scripts or gh3tex.find_script_library()
    tokq = gh3tex._script_material_tokens(script_root) if script_root else {}
    print(f"script tokens: {len(tokq)} (from {script_root})")

    names, stats = {}, {"dbg": 0, "material": 0}
    for platform, root, dbg in DEFAULT_SOURCES:
        if not Path(root).exists():
            print(f"skip {platform}: {root} missing"); continue
        before = len(names)
        collect(platform, root, dbg, tokq, names, stats)
        print(f"{platform}: +{len(names)-before} names (total {len(names)})")

    out = {f"0x{k:08x}": v for k, v in sorted(names.items())}
    args.out.write_text(json.dumps(out, indent=0))
    print(f"\nwrote {len(out)} names -> {args.out}  ({args.out.stat().st_size//1024} KB)")
    print(f"  from dbg: {stats['dbg']}, from material bridge: {stats['material']}")


if __name__ == "__main__":
    sys.exit(main())
