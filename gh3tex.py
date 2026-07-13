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
import hashlib, io, json, re, struct, sys, zlib
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.x360pak._consult_single import (
    SplitPak, XboxTex, TYPE_TEX, PakEntry,
    dds_info, top_mip_size, xbox_storage_size,
    tile_xbox360_dxt, untile_xbox360_dxt, write_dds_bytes, DDS_HEADER_SIZE, u16be,
    make_meta_record, make_chunk_record, TEX_META_RECORD_SIZE, TEX_CHUNK_RECORD_SIZE,
)

TYPE_IMG = 0xdad5e950
TYPE_IMV = 0xb065c9a2            # qk('.imv') - PS3 img pixel data (in the VRAM pak)
TYPE_TVX = 0xea151f1c            # qk('.tvx') - PS3 tex pixel data (in the VRAM pak)
TYPE_LAST = 0x2cb3ef3b
PIL_PIXFMT = {"DXT1": "DXT1", "DXT5": "DXT5"}
_IMG_TYPE_FOURCC = {0x52: "DXT1", 0x53: "DXT5", 0x54: "DXT5", 0x71: "ATI2"}
_PS3_FMT_FOURCC = {1: "DXT1", 2: "DXT1", 5: "DXT5"}   # header byte 0x16 (also bpp@0x15: 4/8)
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


def pc_tex_records(container: bytes):
    """Yield (checksum, w, h, dds_offset, size) for a PC FACECAA7 .tex container.
    PC records are 0x28 bytes (magic 0x0A28) and point at an embedded LE DDS —
    unlike the console 0x30-byte records that hold tiled DXT."""
    if len(container) < 0x10 or container[:4] != b"\xfa\xce\xca\xa7":
        return
    count = u16be(container, 6)
    recs_off = struct.unpack_from(">I", container, 8)[0]
    for i in range(count):
        r = container[recs_off + i*0x28: recs_off + (i+1)*0x28]
        if len(r) < 0x28 or r[:2] != b"\x0a\x28":
            break
        checksum = struct.unpack_from(">I", r, 4)[0]
        w, h = u16be(r, 8), u16be(r, 10)
        off = struct.unpack_from(">I", r, 0x1C)[0]
        size = struct.unpack_from(">I", r, 0x20)[0]
        yield checksum, w, h, off, size


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
# PS3 (RSX): NAME.PAK.PS3 [+ NAME.PAB.PS3] + NAME_VRAM.PAK.PS3, uncompressed.
# Pixels are plain LINEAR little-endian DXT (Pillow-native, no tiling/swap).
# The VRAM pak is self-describing: 32-byte big-endian entry headers (same
# layout as pak headers, full_name at word 6), payload at header_off + start.
# .img headers (0x80 bytes, in PAB) carry dims @+8/+A BE and format @+0x16
# (1/2=DXT1, 5=DXT5). .tex (FACECAA7) containers hold 0x30-byte records:
# checksum@+4, w/h@+8/+A, mips@+0x14, fmt@+0x16, tvxOff@+0x1C, size@+0x20,
# with tvxOff indexing into the paired .tvx VRAM payload (top mip first).
# --------------------------------------------------------------------------- #
class Ps3Pak:
    def __init__(self, pak_path: Path):
        self.pak_path = pak_path
        stem = pak_path.name
        low = stem.lower()
        assert low.endswith('.pak.ps3')
        self.name = stem[:-8].rstrip('.')                     # e.g. GLOBAL
        self.pab_path = pak_path.with_name(f"{self.name}.PAB.PS3")
        self.vram_path = pak_path.with_name(f"{self.name}_VRAM.PAK.PS3")
        self.pak = pak_path.read_bytes()
        self.pab = self.pab_path.read_bytes() if self.pab_path.exists() else b""
        self.vram = self.vram_path.read_bytes() if self.vram_path.exists() else b""
        self.entries = []
        for off in range(0, len(self.pak), 32):
            e = PakEntry.from_header(len(self.entries), off, self.pak[off:off+32])
            self.entries.append(e)
            if e.is_last:
                break
        # VRAM directory: full_name -> (absolute payload offset, size)
        self.vram_dir = {}
        off = 0
        while off + 32 <= len(self.vram):
            t, st, sz = struct.unpack_from(">3I", self.vram, off)
            if t == TYPE_LAST:
                break
            fn = struct.unpack_from(">I", self.vram, off + 24)[0]
            if t in (TYPE_IMV, TYPE_TVX):
                self.vram_dir[(t, fn)] = (off + st, sz)
            off += 32

    def entry_data(self, e) -> bytes:
        """Data for a PAB-resident entry (img headers, tex containers...)."""
        if self.pab:
            off = e.header_start + e.start - len(self.pak)
        else:
            off = e.header_start + e.start                    # monolithic zone pak
        src = self.pab if self.pab else self.pak
        return src[off:off + e.size]


def ps3_img_info(hdr: bytes):
    """(w, h, mips, fourcc) from a PS3 0x80-byte .img header."""
    w, h = u16be(hdr, 8), u16be(hdr, 10)
    mips = hdr[0x14] if len(hdr) > 0x16 else 1
    fourcc = _PS3_FMT_FOURCC.get(hdr[0x16], "?") if len(hdr) > 0x16 else "?"
    return w, h, mips, fourcc


def ps3_tex_records(container: bytes):
    """Yield (checksum, w, h, mips, fourcc, tvx_off, size) from a FACECAA7 tex."""
    if len(container) < 0x10 or container[:4] != b"\xfa\xce\xca\xa7":
        return
    count = u16be(container, 6)
    recs_off = struct.unpack_from(">I", container, 8)[0]
    for i in range(count):
        r = container[recs_off + i*0x30: recs_off + (i+1)*0x30]
        if len(r) < 0x30:
            break
        checksum = struct.unpack_from(">I", r, 4)[0]
        w, h = u16be(r, 8), u16be(r, 10)
        mips, fourcc = r[0x14], _PS3_FMT_FOURCC.get(r[0x16], "?")
        tvx_off = struct.unpack_from(">I", r, 0x1C)[0]
        size = struct.unpack_from(">I", r, 0x20)[0]
        yield checksum, w, h, mips, fourcc, tvx_off, size


def _ps3_image(linear: bytes, w: int, h: int, fourcc: str):
    try:
        return Image.open(io.BytesIO(write_dds_bytes(w, h, fourcc, linear))).convert("RGBA")
    except Exception:
        return None


def _mip_downsample(im: Image.Image, dw: int, dh: int) -> Image.Image:
    """Downsample the top image to a mip level. BOX (area-average) matches the
    Neversoft box-reduce the game used and avoids LANCZOS edge-ringing crust."""
    return im if im.size == (dw, dh) else im.resize((dw, dh), Image.BOX)


def _texconv_encode_chain(levels, fourcc: str):
    """Encode a list of already-downsampled RGBA mip images to a linear DXT chain
    using the bundled texconv.exe (DirectXTex compressor -- cleaner blocks / far
    less mip banding than Pillow). Downsampling is done by the caller (Pillow BOX),
    so texconv only compresses each level as-is (-m 1, no resample). Returns the
    concatenated linear chain, or None if texconv is missing/failed (caller falls
    back to Pillow)."""
    exe = Path(__file__).resolve().parent / "bin" / "texconv.exe"
    if not exe.exists():
        return None
    import subprocess, tempfile, shutil
    td = Path(tempfile.mkdtemp())
    try:
        for i, lvl in enumerate(levels):
            lvl.save(td / f"L{i}.png")
        names = [f"L{i}.png" for i in range(len(levels))]
        try:
            r = subprocess.run([str(exe), "-f", "DXT5" if fourcc == "DXT5" else "DXT1",
                                "-m", "1", "-o", str(td), "-y", *names],
                               cwd=str(td), capture_output=True)
        except OSError:
            return None  # exe present but failed to launch (e.g. missing VC++ runtime)
        if r.returncode != 0:
            return None
        out = bytearray()
        for i, lvl in enumerate(levels):
            dds = None
            for ext in ("DDS", "dds"):
                p = td / f"L{i}.{ext}"
                if p.exists():
                    dds = p.read_bytes(); break
            if dds is None:
                return None
            hdr = 128 + (20 if dds[84:88] == b"DX10" else 0)
            out += dds[hdr:hdr + top_mip_size(lvl.width, lvl.height, fourcc)]
        return bytes(out)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _gen_mipchain_le(im: Image.Image, w: int, h: int, fourcc: str, mips: int) -> bytes:
    """Full linear little-endian DXT mip chain (top first), each level downsampled
    from the top image. Shared by PS3 (written straight to VRAM) and 360 (fed to
    xgtool for correct tiling incl. the packed mip tail)."""
    levels = [_mip_downsample(im, max(1, w >> i), max(1, h >> i)) for i in range(mips)]
    tc = _texconv_encode_chain(levels, fourcc)
    if tc is not None:
        return tc
    pf = PIL_PIXFMT[fourcc]
    out = bytearray()
    for lvl in levels:
        buf = io.BytesIO(); lvl.save(buf, format="DDS", pixel_format=pf)
        out += buf.getvalue()[DDS_HEADER_SIZE:DDS_HEADER_SIZE + top_mip_size(lvl.width, lvl.height, fourcc)]
    return bytes(out)


def _encode_ps3_mipchain(im, w, h, fourcc, mips):
    return _gen_mipchain_le(im, w, h, fourcc, mips)


def _xgtool_tile(chain_le: bytes, base_record: bytes, w: int, h: int, mips: int, fourcc: str):
    """Tile a linear DXT mip chain into a 360 record with the bundled xgtool.exe
    (real XGraphics -> exact tiling + packed mip tail). Returns the tiled record,
    or None if xgtool is missing/failed (caller falls back to top-mip-only)."""
    exe = Path(__file__).resolve().parent / "bin" / "xgtool.exe"
    if not exe.exists():
        return None
    import subprocess, tempfile, shutil
    td = Path(tempfile.mkdtemp())
    try:
        (td / "lin").write_bytes(chain_le)
        (td / "base").write_bytes(base_record)
        r = subprocess.run([str(exe), "tile", str(w), str(h), str(mips),
                            "dxt5" if fourcc == "DXT5" else "dxt1",
                            str(td / "lin"), str(td / "base"), str(td / "out")],
                           capture_output=True)
        if r.returncode != 0 or not (td / "out").exists():
            return None
        out = (td / "out").read_bytes()
        return out if len(out) == len(base_record) else None
    finally:
        shutil.rmtree(td, ignore_errors=True)


def iter_textures_ps3(pak: Ps3Pak):
    """Yield (key, kind, w, h, PIL_image, repack_info) for a PS3 pak.
    repack_info records the absolute VRAM-file offset + byte count to overwrite."""
    for e in pak.entries:
        if e.type == TYPE_IMG:
            hdr = pak.entry_data(e)
            w, h, mips, fourcc = ps3_img_info(hdr)
            loc = pak.vram_dir.get((TYPE_IMV, e.full_name))
            if fourcc not in PIL_PIXFMT or not loc or not w or not h:
                continue
            voff, vsz = loc
            top = top_mip_size(w, h, fourcc)
            if vsz < top or voff + top > len(pak.vram):
                continue
            img = _ps3_image(pak.vram[voff:voff + top], w, h, fourcc)
            if img is not None:
                yield (e.full_name, "img", w, h, img,
                       {"vramOffset": voff, "size": top, "fourCC": fourcc})
        elif e.type == TYPE_TEX:
            loc = pak.vram_dir.get((TYPE_TVX, e.full_name))
            if not loc:
                continue
            tvx_off, tvx_sz = loc
            for checksum, w, h, mips, fourcc, roff, rsz in ps3_tex_records(pak.entry_data(e)):
                if fourcc not in PIL_PIXFMT or not w or not h:
                    continue
                top = top_mip_size(w, h, fourcc)
                if rsz < top or roff + top > tvx_sz:
                    continue
                voff = tvx_off + roff
                img = _ps3_image(pak.vram[voff:voff + top], w, h, fourcc)
                if img is not None:
                    yield (checksum, "tex", w, h, img,
                           {"vramOffset": voff, "size": top, "fourCC": fourcc,
                            "container": e.full_name})


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
                for cs, w, h, off, size in pc_tex_records(e.data):
                    img = _pc_image(e.data[off:off + size])
                    if img is not None:
                        yield (cs, "tex", img.width, img.height, img, {"container": e.full_name})
                continue
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
                       {"entryIndex": e.index, "dataOffset": r.data_offset, "size": r.size, "fourCC": r.fourcc,
                        "container": e.full_name})


# --------------------------------------------------------------------------- #
# real texture names (dbg.pak debug-checksum table)
# --------------------------------------------------------------------------- #
def load_dbg_names(dbg_path) -> dict:
    """Parse a dbg.pak (any platform; decompresses 360 automatically) into a
    {checksum: name} map. dbg entries are plaintext '0xXXXXXXXX <path/name>' lines."""
    data = Path(dbg_path).read_bytes()
    try:
        data = decompress_pak_data(data)
    except Exception:
        pass
    names = {}
    for m in re.finditer(rb'0x([0-9a-fA-F]{8}) ([ -~]{1,220}?)[\r\n]', data):
        names.setdefault(int(m.group(1), 16), m.group(2).decode('latin1').strip())
    return names


def find_dbg_pak(pak_path) -> Path | None:
    """Look for a dbg.pak beside the source pak (its folder, or a sibling PAK dir)."""
    pak_path = Path(pak_path)
    for d in (pak_path.parent, pak_path.parent.parent / "PAK",
              pak_path.parent.parent / "COMPRESSED" / "PAK"):
        if not d.exists():
            continue
        for c in d.iterdir():
            if c.is_file() and c.name.lower() in ("dbg.pak.xen", "dbg.pak.ps3"):
                return c
    return None


def _dbg_stem(path: str):
    """'c:/gh3/data/models/guitars/guitar_skin_dragon.tvx.ps3' -> 'guitar_skin_dragon'."""
    base = re.split(r'[\\/]', path.strip())[-1]
    base = re.sub(r'\.(ps3|xen)$', '', base, flags=re.I)
    base = re.sub(r'\.(tex|tvx|img|imv|dds|bmp)$', '', base, flags=re.I)
    base = re.sub(r'[^0-9A-Za-z._-]', '_', base)
    return base or None


# --- individual textures: material-name bridge (script name -> SCN material -> tex) --
SCN_TYPE = 0x2C3B5ADC


def qk(s: str) -> int:
    """Neversoft QbKey: crc32 of the lowercased, '/'->'\\' normalized string, inverted."""
    return (zlib.crc32(s.lower().replace('/', '\\').encode()) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def find_script_library(start=None):
    """Locate a neversoft-script-library/gh3 tree by walking up from start/cwd."""
    seen = []
    for base in (Path(start) if start else None, Path.cwd(), Path(__file__).resolve().parent):
        if base is None:
            continue
        for d in (base, *base.parents):
            cand = d / "neversoft-script-library" / "gh3"
            if cand.is_dir():
                return cand
    return None


_SCRIPT_TOKEN_CACHE = {}


def _script_material_tokens(script_root) -> dict:
    """{qk(token): token} for every identifier in the .q/.qb scripts (cached per root)."""
    root = Path(script_root)
    key = str(root)
    if key in _SCRIPT_TOKEN_CACHE:
        return _SCRIPT_TOKEN_CACHE[key]
    toks = set()
    for f in root.rglob("*.q*"):
        if f.suffix.lower() not in (".q", ".qb"):
            continue
        try:
            t = f.read_text(errors="replace")
        except Exception:
            continue
        for m in re.finditer(r'[A-Za-z_][A-Za-z0-9_]{2,80}', t):
            toks.add(m.group(0))
    tokq = {qk(t): t for t in toks}
    _SCRIPT_TOKEN_CACHE[key] = tokq
    return tokq


def _collapse_material(name: str) -> str:
    """'sys_gem2d_yellow_sys_gem2d_yellow' -> 'sys_gem2d_yellow' (material = tex_tex)."""
    h = (len(name) - 1) // 2
    if len(name) % 2 == 1 and name[:h] == name[h + 1:]:
        return name[:h]
    return name


def material_texture_names(scn_blobs, tex_checksums, tokq, big_endian=True) -> dict:
    """Map tex-record checksum -> texture name, by reading SCN material records
    (0410 section, records size-chained at +0xE4) and matching each material id to a
    script token. Only single-texture materials are used (unambiguous)."""
    fmt = ">I" if big_endian else "<I"
    fmtH = ">H" if big_endian else "<H"
    out = {}
    for d in scn_blobs:
        if len(d) < 0x24 or struct.unpack_from(fmtH, d, 0x20)[0] != 0x0410:
            continue
        count = struct.unpack_from(fmtH, d, 0x22)[0]
        off = 0x30
        for _ in range(count):
            if off + 0xE8 > len(d):
                break
            size = struct.unpack_from(fmt, d, off + 0xE4)[0]
            if not 0x40 <= size <= 0x4000 or off + size > len(d):
                break
            rec = d[off:off + size]
            mid = struct.unpack_from(fmt, rec, 0)[0]
            refs = [struct.unpack_from(fmt, rec, o)[0] for o in range(0, size - 3, 4)]
            real = [w for w in refs if w in tex_checksums]
            if len(real) == 1 and mid in tokq and real[0] not in out:
                out[real[0]] = _collapse_material(tokq[mid])
            off += size
    return out


def _scn_fmt(d):
    """(u32, u16, f32) struct formats for an SCN, auto-detected from the 0x0410
    section tag at 0x20 (GH3 keeps this metadata big-endian on 360/PS3 and even on
    PC — only pixel DDS is little-endian). None if d is not a 0410 SCN."""
    if len(d) < 0x24:
        return None
    if struct.unpack_from(">H", d, 0x20)[0] == 0x0410:
        return ">I", ">H", ">f"
    if struct.unpack_from("<H", d, 0x20)[0] == 0x0410:
        return "<I", "<H", "<f"
    return None


def _scn_nodes(d):
    """Walk an SCN (0410 section) into editable nodes: id (+name if known), the
    textures it references, and coordinate-like transform floats with their absolute
    byte offsets. Returns None if d is not a 0410 SCN. Backs the scn/*.json pivot
    dump so users can nudge HUD element positions (e.g. the sidebar) and repack them."""
    fmts = _scn_fmt(d)
    if not fmts:
        return None
    fmt, fmtH, fmtF = fmts
    known = bundled_names()
    count = struct.unpack_from(fmtH, d, 0x22)[0]
    off, nodes = 0x30, []
    for _ in range(count):
        if off + 0xE8 > len(d):
            break
        size = struct.unpack_from(fmt, d, off + 0xE4)[0]
        if not 0x40 <= size <= 0x8000 or off + size > len(d):
            break
        nid = struct.unpack_from(fmt, d, off)[0]
        texs, floats = [], []
        for j in range(4, size - 3, 4):
            w = struct.unpack_from(fmt, d, off + j)[0]
            if isinstance(known.get(w), str):
                texs.append(known[w])
            fv = struct.unpack_from(fmtF, d, off + j)[0]
            # coord-like float: finite, non-zero, transform-range magnitude, not a checksum
            if fv == fv and abs(fv) != float("inf") and 1e-2 <= abs(fv) <= 1e5 and (abs(fv - round(fv)) < 1e-3 or abs(fv) < 10):
                floats.append({"off": hex(off + j), "v": fv})   # full float32 value, no rounding
        node = {"i": len(nodes), "id": f"0x{nid:08x}"}
        if isinstance(known.get(nid), str):
            node["name"] = known[nid]
        if texs:
            node["textures"] = texs
        node["floats"] = floats
        nodes.append(node)
        off += size
    return nodes


def _scn_label(name, full_name):
    """Filename stem for an SCN entry: its leaf name (global_gfx.scn -> global_gfx) or hex."""
    if isinstance(name, str) and name:
        stem = re.split(r"[\\/]", name)[-1].split(".scn")[0].split(".")[0]
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
        if stem:
            return stem
    return f"0x{full_name:08x}"


def _write_scn_pivots(out, scn_entries, log):
    """Write scn/<name>.json for each SCN entry (list of (index, full_name, data)).
    Returns {rel_json: {fullName, entryIndex}} for the manifest."""
    names = bundled_names()
    man = {}
    for idx, fn, d in scn_entries:
        nodes = _scn_nodes(d)
        if not nodes:
            continue
        (out / "scn").mkdir(parents=True, exist_ok=True)
        rel = f"scn/{_scn_label(names.get(fn), fn)}.json"
        (out / rel).write_text(json.dumps(
            {"fullName": f"0x{fn:08x}", "entryIndex": idx, "nodes": nodes}, indent=1))
        man[rel] = {"fullName": f"0x{fn:08x}", "entryIndex": idx}
    if man:
        log(f"wrote {len(man)} SCN pivot file(s) to scn/ (edit node floats, repack applies)")
    return man


def _apply_scn_pivots(work, ent, pair, touched, skipped, log):
    """Patch edited scn/*.json transform floats back into their SCN entries in place
    (same size -> no reflow). Returns the number of floats changed. `ent` maps entry
    index -> mutable bytearray; `pair.entries` supplies the SCN entry indices."""
    scn_dir = Path(work) / "scn"
    if not scn_dir.exists():
        return 0
    by_fn = {e.full_name: e.index for e in pair.entries if getattr(e, "type", None) == SCN_TYPE}
    total = 0
    for jf in sorted(scn_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text())
        except Exception:
            skipped.append((f"scn/{jf.name}", "bad json")); continue
        eidx = by_fn.get(int(data.get("fullName", "0"), 16), data.get("entryIndex"))
        if eidx is None or eidx not in ent:
            skipped.append((f"scn/{jf.name}", "scn entry not found")); continue
        buf = ent[eidx]; changed = 0
        fmts = _scn_fmt(buf)
        if not fmts:
            skipped.append((f"scn/{jf.name}", "not a 0410 SCN")); continue
        fmtF = fmts[2]
        for node in data.get("nodes", []):
            for f in node.get("floats", []):
                off = int(f["off"], 16) if isinstance(f["off"], str) else int(f["off"])
                if off + 4 > len(buf):
                    continue
                newv = float(f["v"])
                # dump stores the exact float32 value, so an untouched entry compares
                # equal (bit-exact round-trip) and only real edits patch.
                if struct.unpack_from(fmtF, buf, off)[0] != newv:
                    struct.pack_into(fmtF, buf, off, newv); changed += 1
        if changed:
            touched.add(eidx); total += changed
            log(f"  scn {jf.stem}: patched {changed} transform float(s)")
    return total


_BUNDLED_NAMES = None


def bundled_names() -> dict:
    """The shipped checksum->name map (texture_names.json beside this file)."""
    global _BUNDLED_NAMES
    if _BUNDLED_NAMES is None:
        p = Path(__file__).resolve().parent / "texture_names.json"
        try:
            _BUNDLED_NAMES = {int(k, 16): v for k, v in json.loads(p.read_text()).items()}
        except Exception:
            _BUNDLED_NAMES = {}
    return _BUNDLED_NAMES


def _resolve_names(pak_path, dbg, names, log):
    """Build the {checksum:name} map: the bundled JSON, plus any dbg.pak found next to
    the source (a dbg supplements/overrides). An explicit `names` dict wins outright."""
    if names is not None:
        return names
    out = dict(bundled_names())
    if dbg == "-":                              # explicit: no dbg supplement
        dbg = None
    elif dbg is None:
        dbg = find_dbg_pak(pak_path)
        if dbg:
            log(f"found extra names: {Path(dbg).name}")
    if dbg is not None:
        try:
            out.update(load_dbg_names(dbg))
        except Exception as e:
            log(f"  (couldn't read dbg names: {e})")
    if out:
        log(f"name table: {len(out)} known textures")
    return out


def _png_relpath(kind, key, info, names, multi, used):
    """Pick a PNG path: real name from dbg/material bridge when known, else 0xchecksum.
    A record's own name (material bridge / img) wins; else the dictionary path groups it."""
    container = info.get("container", key)
    rec_name = names.get(key)                        # this texture's own name
    cont_name = names.get(container) if container != key else rec_name
    rec_stem = _dbg_stem(rec_name) if rec_name else None
    cont_stem = _dbg_stem(cont_name) if cont_name else None
    if kind == "img":
        rel = f"img/{rec_stem}" if rec_stem else f"img/0x{key:08x}"
    else:
        leaf = rec_stem or f"0x{key:08x}"
        rel = f"tex/{cont_stem}/{leaf}" if (cont_stem and multi) else f"tex/{leaf}"
    if rel in used:                                  # same stem from a different container
        rel = f"{rel}_0x{key:08x}"
    used.add(rel)
    return rel + ".png", (rec_name or cont_name)


def _png_path(work: Path, mkey: str, info: dict) -> Path:
    """Locate a texture PNG for repack, honoring a manifest 'png' path (old = mkey.png)."""
    return work / info.get("png", f"{mkey}.png")


def _add_material_names(names, scn_blobs, items, platform, scripts, log):
    """Enrich `names` via the SCN material bridge. Only runs when a script library is
    explicitly given -- the bundled name table already carries these, so this is just a
    dev/override path for regenerating or naming a pak the bundle doesn't cover."""
    if scripts is None:
        return names
    tex_cs = {key for key, kind, *_ in items if kind == "tex"}
    if not tex_cs or not scn_blobs:
        return names
    root = scripts if Path(scripts).is_dir() else find_script_library()
    if not root or not Path(root).is_dir():
        return names
    tokq = _script_material_tokens(root)
    mat = material_texture_names(scn_blobs, tex_cs, tokq, big_endian=(platform != "pc"))
    if mat:
        log(f"named {len(mat)} textures via script materials ({Path(root).name})")
        for k, v in mat.items():
            names.setdefault(k, v)
    return names


# --------------------------------------------------------------------------- #
# unpack / repack
# --------------------------------------------------------------------------- #
def _sibling_pab(pak_path: Path) -> Path:
    return pak_path.with_name(pak_path.name.replace(".pak.", ".pab."))


def _is_ps3_pak(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith(".pak.ps3") and not n.endswith("_vram.pak.ps3")


def resolve_pak_path(p) -> Path:
    """Accept a .pak.xen / .PAK.PS3 file, a folder holding one, or a name sans ext."""
    p = Path(p)
    if p.is_dir():
        cands = sorted(p.glob("*.pak.xen")) + sorted(x for x in p.glob("*.[pP][aA][kK].[pP][sS]3") if _is_ps3_pak(x))
        if not cands:
            raise FileNotFoundError(f"no *.pak.xen or *.PAK.PS3 found in folder: {p}")
        return cands[0]
    if p.is_file():
        return p
    for suf in (".pak.xen", ".PAK.PS3"):
        for cand in (Path(str(p) + suf), p.with_name(p.name + suf)):
            if cand.is_file():
                return cand
    raise FileNotFoundError(f"not a .pak.xen/.PAK.PS3 file or a folder containing one: {p}")


def _load_pair(pak_path: Path):
    """Decompress the pak + its .pab sibling in memory and parse (no files written)."""
    pab_path = _sibling_pab(pak_path)
    pak = decompress_pak_data(pak_path.read_bytes())
    pab = decompress_pak_data(pab_path.read_bytes()) if pab_path.exists() else b""
    compressed = len(pak) != pak_path.stat().st_size
    return SplitPak.from_bytes(pak, pab, pak_path=pak_path, pab_path=pab_path), compressed


def unpack(pak_input, out_dir=None, platform=None, log=print, dbg=None, names=None, scripts=None) -> int:
    pak_path = resolve_pak_path(pak_input)
    name = pak_path.name.split(".")[0]
    out = Path(out_dir) if out_dir else pak_path.parent   # default: the input folder
    (out / "tex").mkdir(parents=True, exist_ok=True)
    (out / "img").mkdir(parents=True, exist_ok=True)

    log(f"reading {pak_path.name}")
    if pak_path.name.lower().endswith(".pak.ps3"):
        return _unpack_ps3(pak_path, out, name, log, dbg=dbg, names=names, scripts=scripts)
    pair, compressed = _load_pair(pak_path)
    if compressed:
        log("decompressed (raw DEFLATE) in memory")
    if platform is None:
        platform = detect_platform(pair)
    log(f"platform: {platform}")
    names = _resolve_names(pak_path, dbg, names, log)

    manifest = {"name": name, "platform": platform, "sourcePak": pak_path.name,
                "sourceDir": str(pak_path.parent), "textures": {}}
    items = list(iter_textures(pair, platform))
    names = _add_material_names(names, [e.data for e in pair.entries if getattr(e, "type", None) == SCN_TYPE],
                                items, platform, scripts, log)
    from collections import Counter
    counts = Counter(info.get("container", key) for key, kind, w, h, img, info in items)
    used, n, named = set(), 0, 0
    for key, kind, w, h, img, info in items:
        mkey = f"{kind}/0x{key:08x}"                       # tex/... or img/... (stable repack key)
        if mkey in manifest["textures"]:
            mkey = f"{mkey}_{info.get('entryIndex', 'x')}"  # rare cross-entry key collision
        rel, full = _png_relpath(kind, key, info, names, counts[info.get("container", key)] > 1, used)
        png = out / rel
        png.parent.mkdir(parents=True, exist_ok=True)
        img.save(png, "PNG")
        entry = {"kind": kind, "w": w, "h": h, "pngSha256": sha(png.read_bytes()), "png": rel, **info}
        if full:
            entry["name"] = full; named += 1
        manifest["textures"][mkey] = entry
        n += 1
        if n % 100 == 0:
            log(f"  ...{n} textures")
    if names:
        log(f"named {named}/{n} textures (rest keep their checksum)")
    manifest["scn"] = _write_scn_pivots(
        out, [(e.index, e.full_name, e.data) for e in pair.entries
              if getattr(e, "type", None) == SCN_TYPE], log)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    nt = sum(1 for v in manifest["textures"].values() if v["kind"] == "tex")
    log(f"unpacked {n} {platform} textures ({nt} in tex/, {n - nt} in img/) -> {out}")
    if platform == "pc":
        log("  (PC unpack is decode-only: copy these PNGs into an Xbox unpack folder, then repack that)")
    return n


def _unpack_ps3(pak_path: Path, out: Path, name: str, log=print, dbg=None, names=None, scripts=None) -> int:
    pak = Ps3Pak(pak_path)
    if not pak.vram:
        raise FileNotFoundError(f"missing VRAM pak: {pak.vram_path.name} (PS3 pixels live there)")
    log(f"platform: ps3  ({pak.pab_path.name if pak.pab else 'monolithic'} + {pak.vram_path.name})")
    names = _resolve_names(pak_path, dbg, names, log)
    manifest = {"name": name, "platform": "ps3", "sourcePak": pak_path.name,
                "sourceDir": str(pak_path.parent), "vramPak": pak.vram_path.name,
                "textures": {}}
    items = list(iter_textures_ps3(pak))
    names = _add_material_names(names, [pak.entry_data(e) for e in pak.entries if e.type == SCN_TYPE],
                                items, "ps3", scripts, log)
    from collections import Counter
    counts = Counter(info.get("container", key) for key, kind, w, h, img, info in items)
    used, n, named = set(), 0, 0
    for key, kind, w, h, img, info in items:
        mkey = f"{kind}/0x{key:08x}"
        if mkey in manifest["textures"]:
            mkey = f"{mkey}_{info['vramOffset']:x}"
        rel, full = _png_relpath(kind, key, info, names, counts[info.get("container", key)] > 1, used)
        png = out / rel
        png.parent.mkdir(parents=True, exist_ok=True)
        img.save(png, "PNG")
        entry = {"kind": kind, "w": w, "h": h, "pngSha256": sha(png.read_bytes()), "png": rel, **info}
        if full:
            entry["name"] = full; named += 1
        manifest["textures"][mkey] = entry
        n += 1
        if n % 100 == 0:
            log(f"  ...{n} textures")
    if names:
        log(f"named {named}/{n} textures (rest keep their checksum)")
    manifest["scn"] = _write_scn_pivots(
        out, [(e.index, e.full_name, pak.entry_data(e)) for e in pak.entries
              if getattr(e, "type", None) == SCN_TYPE], log)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    nt = sum(1 for v in manifest["textures"].values() if v["kind"] == "tex")
    log(f"unpacked {n} ps3 textures ({nt} in tex/, {n - nt} in img/) -> {out}")
    return n


def _repack_ps3(work: Path, manifest: dict, out_pak, log=print) -> int:
    name = manifest["name"]
    src_pak = _resolve_source(work, manifest)
    pak = Ps3Pak(src_pak)
    if not pak.vram:
        raise FileNotFoundError(f"missing VRAM pak next to {src_pak}")
    vram = bytearray(pak.vram)
    # mip layout per texture checksum from the SOURCE pak, so multi-mip textures get
    # their whole chain rewritten (old manifests lacking mip fields are fixed too).
    mipinfo = {}
    for e in pak.entries:
        if e.type == TYPE_TEX:
            for cs, tw, th, mips, fc, roff, rsz in ps3_tex_records(pak.entry_data(e)):
                mipinfo[cs] = (mips, rsz)
    edited, unchanged, skipped = 0, 0, []
    for mkey, info in manifest["textures"].items():
        png = _png_path(work, mkey, info)
        if not png.exists():
            skipped.append((mkey, "png missing")); continue
        if sha(png.read_bytes()) == info["pngSha256"]:
            unchanged += 1; continue
        w, h, fourcc = info["w"], info["h"], info["fourCC"]
        im = Image.open(png).convert("RGBA")
        if im.size != (w, h):
            log(f"  resized {mkey} {im.width}x{im.height} -> {w}x{h}")
            im = im.resize((w, h), Image.LANCZOS)
        cs = int(mkey.split("0x")[-1], 16) if "0x" in mkey else None
        mips, chain_size = mipinfo.get(cs, (1, info["size"]))
        if mips > 1:
            linear = _encode_ps3_mipchain(im, w, h, fourcc, mips)
            cap = chain_size
        else:
            linear = _gen_mipchain_le(im, w, h, fourcc, 1)   # texconv + alpha bleed (pillow fallback)
            cap = info["size"]
        n = min(len(linear), cap)
        voff = info["vramOffset"]
        vram[voff:voff + n] = linear[:n]        # linear LE DXT straight in - no transform
        edited += 1
        log(f"  re-encoded {mkey} ({w}x{h} {fourcc}{f', {mips} mips' if mips > 1 else ''})")

    # apply SCN pivot edits: PS3 SCN containers live in the PAB (big-endian floats)
    pab = bytearray(pak.pab) if pak.pab else None
    scn_dir = work / "scn"
    if scn_dir.exists() and pab is not None:
        loc = {e.full_name: (e.header_start + e.start - len(pak.pak), e.size)
               for e in pak.entries if e.type == SCN_TYPE}
        for jf in sorted(scn_dir.glob("*.json")):
            try:
                data = json.loads(jf.read_text())
            except Exception:
                skipped.append((f"scn/{jf.name}", "bad json")); continue
            base_size = loc.get(int(data.get("fullName", "0"), 16))
            if not base_size:
                skipped.append((f"scn/{jf.name}", "scn entry not found")); continue
            base, psize = base_size
            fmts = _scn_fmt(pab[base:base + psize])
            if not fmts:
                skipped.append((f"scn/{jf.name}", "not a 0410 SCN")); continue
            fmtF = fmts[2]; ch = 0
            for node in data.get("nodes", []):
                for f in node.get("floats", []):
                    off = int(f["off"], 16) if isinstance(f["off"], str) else int(f["off"])
                    if off + 4 > psize:
                        continue
                    newv = float(f["v"])
                    if struct.unpack_from(fmtF, pab, base + off)[0] != newv:
                        struct.pack_into(fmtF, pab, base + off, newv); ch += 1
            if ch:
                edited += ch
                log(f"  scn {jf.stem}: patched {ch} transform float(s)")

    if not edited:
        log(f"no edited PNGs (unchanged {unchanged}) - nothing to repack"); return 0
    out_pak = Path(out_pak) if out_pak else work / f"{name}_new.PAK.PS3"
    stem = out_pak.name[:-8].rstrip('.') if out_pak.name.lower().endswith('.pak.ps3') else out_pak.stem
    out_pak.parent.mkdir(parents=True, exist_ok=True)
    out_pak.write_bytes(pak.pak)                                  # header pak verbatim
    if pak.pab:
        out_pak.with_name(f"{stem}.PAB.PS3").write_bytes(bytes(pab))  # pab (SCN edits applied)
    out_pak.with_name(f"{stem}_VRAM.PAK.PS3").write_bytes(bytes(vram))
    log(f"repacked {edited} edited textures (unchanged {unchanged}, skipped {len(skipped)}) -> {out_pak}")
    log(f"  wrote: {out_pak.name}" + (f" + {stem}.PAB.PS3" if pak.pab else "") + f" + {stem}_VRAM.PAK.PS3")
    if skipped:
        log(f"  skipped: {skipped[:6]}{'...' if len(skipped) > 6 else ''}")
    return edited


def _resolve_source(work: Path, manifest: dict) -> Path:
    """Find the original pak recorded in the manifest (folder-local first)."""
    name = manifest["sourcePak"]
    for cand in (work / name, Path(manifest.get("sourceDir", "")) / name):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"can't find the source pak '{name}' (looked next to manifest.json and in "
        f"{manifest.get('sourceDir')!r}). Keep the original .pak.xen/.pab.xen there.")


def _write_xbox_mipchain(dst: bytearray, off: int, im: Image.Image, w: int, h: int,
                         fourcc: str, mip_count: int, rec_size: int) -> bool:
    """Rewrite the whole tiled record (all mips incl. the packed tail) from the edited
    image via xgtool (real XGraphics). Returns True on success, False if xgtool is
    unavailable so the caller can fall back to a top-mip-only write."""
    # need room for the mip allocation beyond the base tile; if the record only holds the
    # base (mip0 fills the whole tile, or a shared/odd record), fall back to top-only.
    if rec_size <= xbox_storage_size(w, h, fourcc):
        return False
    chain = _gen_mipchain_le(im, w, h, fourcc, mip_count)
    tiled = _xgtool_tile(chain, bytes(dst[off:off + rec_size]), w, h, mip_count, fourcc)
    if tiled is None:
        return False
    dst[off:off + rec_size] = tiled
    return True


def _grow_tex_records(container: bytes, edits: dict) -> bytes:
    """Replace one or more records in a FACECAA7 TEX container with new-size art by
    APPENDING each new payload past the existing data and re-pointing only that
    record's meta+chunk. Every other record keeps its exact bytes/offset (mips and
    aliasing intact). Grown records are single-mip. edits: checksum -> (w, h, fourcc,
    linear_top_dxt)."""
    tex = XboxTex.parse(container)
    by_cs = {r.checksum: r for r in tex.records}
    out = bytearray(container)
    for cs, (w, h, fourcc) in ((k, v[:3]) for k, v in edits.items()):
        linear = edits[cs][3]
        r = by_cs[cs]
        payload = tile_xbox360_dxt(linear, w, h, fourcc, word_swap=True)
        out += bytes((-len(out)) % 0x1000)           # align append start
        data_offset = len(out)
        out += payload
        out += bytes((-len(out)) % 0x1000)           # keep container 0x1000-aligned
        rec = make_meta_record(cs, w, h, fourcc, r.chunk_offset, data_offset,
                               len(payload), template=r.raw_record)
        out[r.record_offset:r.record_offset + TEX_META_RECORD_SIZE] = rec
        ch = make_chunk_record(w, h, fourcc, template=r.raw_chunk)
        out[r.chunk_offset:r.chunk_offset + TEX_CHUNK_RECORD_SIZE] = ch
    return bytes(out)


def _grow_img_entry(entry: bytes, w: int, h: int, fourcc: str, linear_top: bytes) -> bytes:
    """Rebuild a standalone .img entry (0x1000 header + tiled payload) at new dims.
    Patches both width/height fields + the size field in the header and rewrites the
    chunk record, then tiles the single-mip payload. Payload lives at 0x1000."""
    out = bytearray(entry[:0x1000])
    struct.pack_into(">HH", out, 8, w, h)            # primary w/h
    struct.pack_into(">HH", out, 0x0e, w, h)         # mirrored w/h
    out[20] = 1                                      # grown payload is single-mip
    struct.pack_into(">I", out, 32, xbox_storage_size(w, h, fourcc))
    ci = out.find(_CHUNK_MAGIC)
    if ci >= 0:
        out[ci:ci + TEX_CHUNK_RECORD_SIZE] = make_chunk_record(
            w, h, fourcc, template=bytes(out[ci:ci + TEX_CHUNK_RECORD_SIZE]))
    out += tile_xbox360_dxt(linear_top, w, h, fourcc, word_swap=True)
    return bytes(out)


def repack(work_dir, out_pak=None, out_pab=None, log=print) -> int:
    work = Path(work_dir)
    manifest = json.loads((work / "manifest.json").read_text())
    plat = manifest.get("platform", "xbox")
    if plat == "ps3":
        return _repack_ps3(work, manifest, out_pak, log)
    if plat != "xbox":
        raise ValueError("this is a PC unpack (decode-only) - copy its PNGs into an "
                         "Xbox unpack folder by matching name and repack that instead")
    name = manifest["name"]
    src = _resolve_source(work, manifest)
    log(f"reading source {src.name}")
    pair, _ = _load_pair(src)
    ent = {e.index: bytearray(e.data) for e in pair.entries}
    # mip count per TEX-record checksum from the source (fixes multi-mip textures whose
    # lower levels would otherwise keep the original art; old manifests handled too).
    mipcount = {}   # checksum -> (mip_count, full_record_size) from the source
    for e in pair.entries:
        if getattr(e, "type", None) == TYPE_TEX:
            try:
                for r in XboxTex.parse(e.data).records:
                    mipcount[r.checksum] = (r.mip_count, r.size)
            except Exception:
                pass
    touched, edited, unchanged, skipped = set(), 0, 0, []
    tex_grow = {}   # container entryIndex -> {checksum: (w, h, fourcc, linear_top)}

    for mkey, info in manifest["textures"].items():
        png = _png_path(work, mkey, info)
        if not png.exists():
            skipped.append((mkey, "png missing")); continue
        if sha(png.read_bytes()) == info["pngSha256"]:
            unchanged += 1; continue     # untouched -> keep original DXT (lossless)
        w, h, fourcc = info["w"], info["h"], info["fourCC"]
        if fourcc not in PIL_PIXFMT:
            skipped.append((mkey, f"{fourcc} has no PNG codec")); continue
        im = Image.open(png).convert("RGBA")
        eidx, off = info["entryIndex"], info["dataOffset"]
        cs = int(mkey.split("0x")[-1], 16) if "0x" in mkey else None

        if im.size != (w, h):
            # replacement at a different resolution: the game reads dims from the header,
            # so re-point the texture to a fresh single-mip payload at the new size (img
            # entries grow in place; tex-container records get appended + reflowed). Any
            # size is honored, incl. 1x1 (a legit trick to hide an element like flames).
            nw, nh = im.size
            linear = _gen_mipchain_le(im, nw, nh, fourcc, 1)
            if mkey.startswith("img/"):
                ent[eidx] = bytearray(_grow_img_entry(bytes(ent[eidx]), nw, nh, fourcc, linear))
                touched.add(eidx); edited += 1
                log(f"  resized {mkey} {w}x{h} -> {nw}x{nh} {fourcc} (single mip)")
            else:
                tex_grow.setdefault(eidx, {})[cs] = (nw, nh, fourcc, linear)
            continue

        if mkey.startswith("tex/"):
            mips, rec_size = mipcount.get(cs, (1, info["size"]))
        else:
            # img entries carry their mip count at header byte 20 (same 40-byte meta
            # layout as tex records); the chain lives in the payload after the top tile.
            mips = ent[eidx][20] if len(ent[eidx]) > 20 else 1
            rec_size = len(ent[eidx]) - off
            if mips <= 1 or rec_size <= xbox_storage_size(w, h, fourcc):
                mips, rec_size = 1, info["size"]
        if mips > 1 and _write_xbox_mipchain(ent[eidx], off, im, w, h, fourcc, mips, rec_size):
            touched.add(eidx); edited += 1
            log(f"  re-encoded {mkey} ({w}x{h} {fourcc}, {mips} mips)")
        else:
            linear = _gen_mipchain_le(im, w, h, fourcc, 1)   # texconv + alpha bleed (pillow fallback)
            stor = xbox_storage_size(w, h, fourcc)
            base = bytes(ent[eidx][off:off + stor])
            payload = tile_xbox360_dxt(linear, w, h, fourcc, word_swap=True, base_data=base)
            nbytes = min(len(payload), info["size"])
            ent[eidx][off:off + nbytes] = payload[:nbytes]
            touched.add(eidx); edited += 1
            log(f"  re-encoded {mkey} ({w}x{h} {fourcc}{' (top only, xgtool missing)' if mips > 1 else ''})")

    # apply tex-container grows last, on top of any in-place edits to the same container
    for eidx, cont_edits in tex_grow.items():
        ent[eidx] = bytearray(_grow_tex_records(bytes(ent[eidx]), cont_edits))
        touched.add(eidx); edited += len(cont_edits)
        for cs, (nw, nh, fc, _lin) in cont_edits.items():
            log(f"  grew tex 0x{cs:08x} -> {nw}x{nh} {fc} (single mip, appended)")

    edited += _apply_scn_pivots(work, ent, pair, touched, skipped, log)

    if not edited:
        log(f"no edited PNGs (unchanged {unchanged}) - nothing to repack"); return 0
    out_pak = Path(out_pak) if out_pak else work / f"{name}_new.pak.xen"
    out_pab = Path(out_pab) if out_pab else Path(str(out_pak).replace(".pak.", ".pab."))
    pair.write_pair(out_pak, out_pab, {i: bytes(ent[i]) for i in touched})
    log(f"repacked {edited} edited textures (unchanged {unchanged}, skipped {len(skipped)}) -> {out_pak}")
    if skipped:
        log(f"  skipped: {skipped[:6]}{'...' if len(skipped) > 6 else ''}")
    return edited
