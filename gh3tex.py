#!/usr/bin/env python3
"""Core library for the GH3 Xbox 360 texture tool (used by unpack.py/repack.py).

Handles the whole path: auto-decompress a compressed zone pak, extract every
texture (TEX records + .img entries) to PNG via Pillow, and repack edited PNGs
back in place. No Honeycomb, no texconv -- decompression is native zlib and
PNG<->DXT is Pillow (a plain DXT<->RGBA convert with no gamma tag, so paint.net
edits don't darken).

A .img entry is a single-texture TEX (0x1000 header + tiled top-mip payload).
An unpack writes a manifest.json recording each texture's source entry + a
baseline hash, so repack re-encodes ONLY what you changed and leaves the rest
byte-identical.
"""
from __future__ import annotations
import hashlib, io, json, struct, sys, zlib
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.x360pak._consult_single import (
    SplitPak, XboxTex, TYPE_TEX,
    dds_info, top_mip_size, xbox_storage_size,
    tile_xbox360_dxt, untile_xbox360_dxt, write_dds_bytes, DDS_HEADER_SIZE, u16be,
)

TYPE_IMG = 0xdad5e950
PIL_PIXFMT = {"DXT1": "DXT1", "DXT5": "DXT5"}
_IMG_TYPE_FOURCC = {0x52: "DXT1", 0x53: "DXT5", 0x54: "DXT5", 0x71: "ATI2"}
_CHUNK_MAGIC = b"\x00\x20\x00\x03\x00\x00\x00\x01"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# compression (native; matches GH-Toolkit-NET Compression.cs)
# --------------------------------------------------------------------------- #
def decompress_pak_data(data: bytes) -> bytes:
    """GH3 360 zone paks are raw-DEFLATE compressed (World Tour uses CHNK).
    Returns the data unchanged if it is already decompressed."""
    if data[:4] == b"CHNK":
        return _decompress_chnk(data)
    try:
        return zlib.decompress(data, -15)   # raw DEFLATE, no zlib header
    except zlib.error:
        return data                          # already decompressed


def _decompress_chnk(data: bytes) -> bytes:
    out = bytearray(); pos = 0
    while True:
        base = pos
        off, comp, nxt_off, _nxt_len, _dec_sz, _dec_off = struct.unpack_from(">6I", data, pos + 4)
        out += zlib.decompress(data[base + off:base + off + comp], -15)
        if nxt_off == 0xFFFFFFFF:
            break
        pos = base + nxt_off
    return bytes(out)


# --------------------------------------------------------------------------- #
# .img header + Pillow PNG<->DXT
# --------------------------------------------------------------------------- #
def img_info(data: bytes):
    """(width, height, fourcc, payload_offset, storage) for a .img entry."""
    w, h = u16be(data, 8), u16be(data, 10)
    ci = data.find(_CHUNK_MAGIC)
    it = data[ci + 35] if 0 <= ci and ci + 35 < len(data) else 0
    fourcc = _IMG_TYPE_FOURCC.get(it, "?")
    stor = xbox_storage_size(w, h, fourcc) if fourcc in ("DXT1", "DXT5") else 0
    return w, h, fourcc, 0x1000, stor


def dds_bytes_to_png(dds_bytes: bytes, png_path: Path):
    Image.open(io.BytesIO(dds_bytes)).save(png_path, "PNG")


def png_to_dds_bytes(png_path: Path, fourcc: str) -> bytes:
    pf = PIL_PIXFMT.get(fourcc)
    if pf is None:
        raise ValueError(f"{png_path.name}: {fourcc} has no PNG codec")
    buf = io.BytesIO()
    Image.open(png_path).convert("RGBA").save(buf, format="DDS", pixel_format=pf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# platform detection + per-platform texture decode
# --------------------------------------------------------------------------- #
def detect_platform(pair: SplitPak) -> str:
    """PC textures embed a standard little-endian DDS in their payload; Xbox
    textures are raw tiled DXT. Sniff a few entries to tell them apart."""
    for e in pair.entries[:64]:
        if getattr(e, "type", None) in (TYPE_IMG, TYPE_TEX) and b"DDS " in e.data[:4096]:
            return "pc"
    return "xbox"


def _xbox_image(tiled: bytes, w: int, h: int, fourcc: str) -> Image.Image:
    linear = untile_xbox360_dxt(tiled, w, h, fourcc, word_swap=True)
    return Image.open(io.BytesIO(write_dds_bytes(w, h, fourcc, linear))).convert("RGBA")


def _pc_image(data: bytes):
    """A PC .img/.tex-record payload is an embedded little-endian DDS."""
    off = data.find(b"DDS ")
    if off < 0:
        return None
    try:
        return Image.open(io.BytesIO(data[off:])).convert("RGBA")
    except Exception:
        return None


def iter_textures(pair: SplitPak, platform: str):
    """Yield (key, kind, w, h, PIL_image, repack_info) for every decodable
    texture. repack_info carries the Xbox in-place location (empty for PC)."""
    for e in pair.entries:
        t = getattr(e, "type", None)
        if t == TYPE_IMG:
            if platform == "pc":
                img = _pc_image(e.data)
                if img is not None:
                    yield (e.full_name, "img", img.width, img.height, img, {})
            else:
                w, h, fourcc, poff, stor = img_info(e.data)
                if fourcc in ("DXT1", "DXT5") and stor and len(e.data) >= poff + stor:
                    yield (e.full_name, "img", w, h, _xbox_image(e.data[poff:poff + stor], w, h, fourcc),
                           {"entryIndex": e.index, "dataOffset": poff, "size": stor, "fourCC": fourcc})
        elif t == TYPE_TEX:
            if platform == "pc":
                continue   # PC TEX atlas (e.g. global_gfx) not yet split into records
            try:
                recs = XboxTex.parse(e.data).records
            except Exception:
                continue
            for r in recs:
                if r.fourcc not in ("DXT1", "DXT5"):
                    continue
                if len(r.data) < xbox_storage_size(r.width, r.height, r.fourcc):
                    continue
                yield (r.checksum, "tex", r.width, r.height, _xbox_image(r.data, r.width, r.height, r.fourcc),
                       {"entryIndex": e.index, "dataOffset": r.data_offset, "size": r.size, "fourCC": r.fourcc})


# --------------------------------------------------------------------------- #
# unpack / repack
# --------------------------------------------------------------------------- #
def _sibling_pab(pak_path: Path) -> Path:
    return pak_path.with_name(pak_path.name.replace(".pak.", ".pab."))


def resolve_pak_path(p) -> Path:
    """Accept a .pak.xen file, a folder holding one, or a name without extension."""
    p = Path(p)
    if p.is_dir():
        cands = sorted(p.glob("*.pak.xen"))
        if not cands:
            raise FileNotFoundError(f"no *.pak.xen found in folder: {p}")
        return cands[0]
    if p.is_file():
        return p
    for cand in (Path(str(p) + ".pak.xen"), p.with_name(p.name + ".pak.xen")):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"not a .pak.xen file or a folder containing one: {p}")


def _load_pair(pak_path: Path):
    """Decompress the pak + its .pab sibling in memory and parse (no files written)."""
    pab_path = _sibling_pab(pak_path)
    pak = decompress_pak_data(pak_path.read_bytes())
    pab = decompress_pak_data(pab_path.read_bytes()) if pab_path.exists() else b""
    compressed = len(pak) != pak_path.stat().st_size
    return SplitPak.from_bytes(pak, pab, pak_path=pak_path, pab_path=pab_path), compressed


def unpack(pak_input, out_dir=None, platform=None, log=print) -> int:
    pak_path = resolve_pak_path(pak_input)
    name = pak_path.name.split(".")[0]
    out = Path(out_dir) if out_dir else pak_path.parent   # default: the input folder
    (out / "tex").mkdir(parents=True, exist_ok=True)
    (out / "img").mkdir(parents=True, exist_ok=True)

    log(f"reading {pak_path.name}")
    pair, compressed = _load_pair(pak_path)
    if compressed:
        log("decompressed (raw DEFLATE) in memory")
    if platform is None:
        platform = detect_platform(pair)
    log(f"platform: {platform}")

    manifest = {"name": name, "platform": platform, "sourcePak": pak_path.name,
                "sourceDir": str(pak_path.parent), "textures": {}}
    n = 0
    for key, kind, w, h, img, info in iter_textures(pair, platform):
        mkey = f"{kind}/0x{key:08x}"                       # tex/... or img/... (own folder)
        if mkey in manifest["textures"]:
            mkey = f"{mkey}_{info.get('entryIndex', 'x')}"  # rare cross-entry key collision
        png = out / f"{mkey}.png"
        img.save(png, "PNG")
        manifest["textures"][mkey] = {"kind": kind, "w": w, "h": h,
                                      "pngSha256": sha(png.read_bytes()), **info}
        n += 1
        if n % 100 == 0:
            log(f"  ...{n} textures")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    nt = sum(1 for v in manifest["textures"].values() if v["kind"] == "tex")
    log(f"unpacked {n} {platform} textures ({nt} in tex/, {n - nt} in img/) -> {out}")
    if platform == "pc":
        log("  (PC unpack is decode-only: copy these PNGs into an Xbox unpack folder, then repack that)")
    return n


def _resolve_source(work: Path, manifest: dict) -> Path:
    """Find the original pak recorded in the manifest (folder-local first)."""
    name = manifest["sourcePak"]
    for cand in (work / name, Path(manifest.get("sourceDir", "")) / name):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"can't find the source pak '{name}' (looked next to manifest.json and in "
        f"{manifest.get('sourceDir')!r}). Keep the original .pak.xen/.pab.xen there.")


def repack(work_dir, out_pak=None, out_pab=None, log=print) -> int:
    work = Path(work_dir)
    manifest = json.loads((work / "manifest.json").read_text())
    if manifest.get("platform", "xbox") != "xbox":
        raise ValueError("this is a PC unpack (decode-only) - copy its PNGs into an "
                         "Xbox unpack folder by matching name and repack that instead")
    name = manifest["name"]
    src = _resolve_source(work, manifest)
    log(f"reading source {src.name}")
    pair, _ = _load_pair(src)
    ent = {e.index: bytearray(e.data) for e in pair.entries}
    touched, edited, unchanged, skipped = set(), 0, 0, []

    for mkey, info in manifest["textures"].items():
        png = work / f"{mkey}.png"
        if not png.exists():
            skipped.append((mkey, "png missing")); continue
        if sha(png.read_bytes()) == info["pngSha256"]:
            unchanged += 1; continue     # untouched -> keep original DXT (lossless)
        w, h, fourcc = info["w"], info["h"], info["fourCC"]
        if fourcc not in PIL_PIXFMT:
            skipped.append((mkey, f"{fourcc} has no PNG codec")); continue
        im = Image.open(png).convert("RGBA")
        if im.size != (w, h):                       # e.g. a merged PC texture of another size
            log(f"  resized {mkey} {im.width}x{im.height} -> {w}x{h}")
            im = im.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, format="DDS", pixel_format=PIL_PIXFMT[fourcc])
        dds = buf.getvalue()
        linear = dds[DDS_HEADER_SIZE:DDS_HEADER_SIZE + top_mip_size(w, h, fourcc)]
        eidx, off = info["entryIndex"], info["dataOffset"]
        stor = xbox_storage_size(w, h, fourcc)
        base = bytes(ent[eidx][off:off + stor])
        payload = tile_xbox360_dxt(linear, w, h, fourcc, word_swap=True, base_data=base)
        nbytes = min(len(payload), info["size"])
        ent[eidx][off:off + nbytes] = payload[:nbytes]
        touched.add(eidx); edited += 1
        log(f"  re-encoded {mkey} ({w}x{h} {fourcc})")

    if not edited:
        log(f"no edited PNGs (unchanged {unchanged}) - nothing to repack"); return 0
    out_pak = Path(out_pak) if out_pak else work / f"{name}_new.pak.xen"
    out_pab = Path(out_pab) if out_pab else Path(str(out_pak).replace(".pak.", ".pab."))
    pair.write_pair(out_pak, out_pab, {i: bytes(ent[i]) for i in touched})
    log(f"repacked {edited} edited textures (unchanged {unchanged}, skipped {len(skipped)}) -> {out_pak}")
    if skipped:
        log(f"  skipped: {skipped[:6]}{'...' if len(skipped) > 6 else ''}")
    return edited
