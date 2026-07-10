#!/usr/bin/env python3
"""
Starter Xbox 360 GH3 split PAK/PAB + TEX builder/validator.

This is intentionally conservative:
  * big-endian GH3/Xenon 32-byte PAK headers only;
  * split PAK/PAB formula: pab_offset = header_start + start_field - pak_size;
  * preserves source PAK header size and padding bytes;
  * preserves entry order and non-offset header fields;
  * supports replacing the global_gfx.tex entry in MISC;
  * supports parsing/rebuilding GH3 Xbox .tex.xen containers from DXT DDS files;
  * defaults to PC-order DDS -> Xbox-native DXT/ATI conversion, including 16-bit word swap.

The first milestone should be: round-trip MISC with no replacements and require byte-identical
PAK and PAB output. Then replace only global_gfx.tex and validate the entry/TEX structure.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FTYPE_LAST = 0x2CB3EF3B
TYPE_TEX = 0x8BFA5E8E
GLOBAL_GFX_TEX_FULLNAME = 0x406D171F

DDS_HEADER_SIZE = 128
TEX_MAGIC = 0xFACECAA7
TEX_VERSION = 0x011C
TEX_META_RECORD_SIZE = 40
TEX_CHUNK_RECORD_SIZE = 0x34

FORMAT_INFO = {
    "DXT1": {"bytes_per_block": 8,  "bpp": 4, "unkD": 0x0100, "img_type": 0x52},
    "DXT5": {"bytes_per_block": 16, "bpp": 8, "unkD": 0x0500, "img_type": 0x54},
    "ATI2": {"bytes_per_block": 16, "bpp": 8, "unkD": 0x7100, "img_type": 0x71},
}


def u32be(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def u16be(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def put_u32be(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into(">I", buf, off, value & 0xFFFFFFFF)


def put_u16be(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into(">H", buf, off, value & 0xFFFF)


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_hex_key(value: str | int) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    s = value.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return int(s, 16) & 0xFFFFFFFF


def hex8(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


@dataclasses.dataclass
class PakEntry:
    index: int
    header_start: int
    type: int
    start: int
    size: int
    pak_full_filename_key: int
    full_name: int
    name_sum: int
    unk: int
    flags: int
    data: bytes = b""
    prepad: bytes = b""

    @property
    def is_last(self) -> bool:
        return self.type == FTYPE_LAST

    def computed_pab_offset(self, pak_size: int) -> int:
        return self.header_start + self.start - pak_size

    @classmethod
    def from_header(cls, index: int, header_start: int, raw: bytes) -> "PakEntry":
        vals = struct.unpack(">8I", raw)
        return cls(index, header_start, *vals)

    def write_header_into(self, out: bytearray, pak_size: int, pab_offset: int, new_size: int) -> None:
        # start_field is not a direct PAB offset. It is the value that makes:
        #   pab_offset = header_start + start_field - pak_size
        start_field = pak_size + pab_offset - self.header_start
        struct.pack_into(
            ">8I", out, self.header_start,
            self.type & 0xFFFFFFFF,
            start_field & 0xFFFFFFFF,
            new_size & 0xFFFFFFFF,
            self.pak_full_filename_key & 0xFFFFFFFF,
            self.full_name & 0xFFFFFFFF,
            self.name_sum & 0xFFFFFFFF,
            self.unk & 0xFFFFFFFF,
            self.flags & 0xFFFFFFFF,
        )


class SplitPak:
    def __init__(self, pak_path: Path, pab_path: Path, pak: bytes, pab: bytes, entries: List[PakEntry], tail: bytes):
        self.pak_path = pak_path
        self.pab_path = pab_path
        self.pak = pak
        self.pab = pab
        self.entries = entries
        self.tail = tail

    @classmethod
    def read(cls, pak_path: Path, pab_path: Optional[Path] = None) -> "SplitPak":
        if pab_path is None:
            pab_path = Path(str(pak_path).replace(".pak.", ".pab."))
        return cls.from_bytes(pak_path.read_bytes(), pab_path.read_bytes(),
                              pak_path=pak_path, pab_path=pab_path)

    @classmethod
    def from_bytes(cls, pak: bytes, pab: bytes, *, pak_path: Optional[Path] = None,
                   pab_path: Optional[Path] = None) -> "SplitPak":
        pak_path = pak_path or Path("mem.pak.xen")
        pab_path = pab_path or Path("mem.pab.xen")
        entries: List[PakEntry] = []

        for off in range(0, len(pak), 32):
            if off + 32 > len(pak):
                break
            entry = PakEntry.from_header(len(entries), off, pak[off:off + 32])
            entries.append(entry)
            if entry.is_last:
                break

        if not entries or not entries[-1].is_last:
            raise ValueError(f"{pak_path} has no .last header before EOF")

        prev_end = 0
        for entry in entries:
            pab_offset = entry.computed_pab_offset(len(pak))
            if pab_offset < 0 or pab_offset + entry.size > len(pab):
                raise ValueError(
                    f"entry {entry.index} points outside PAB: "
                    f"off={pab_offset:#x} size={entry.size:#x} pab_size={len(pab):#x}"
                )
            entry.prepad = pab[prev_end:pab_offset]
            entry.data = pab[pab_offset:pab_offset + entry.size]
            prev_end = pab_offset + entry.size

        tail = pab[prev_end:]
        return cls(pak_path, pab_path, pak, pab, entries, tail)

    def find_entry(self, *, type: Optional[int] = None, full_name: Optional[int] = None) -> PakEntry:
        for entry in self.entries:
            if type is not None and entry.type != type:
                continue
            if full_name is not None and entry.full_name != full_name:
                continue
            return entry
        raise KeyError(f"entry not found type={type!r} full_name={full_name!r}")

    def inspection(self) -> dict:
        intervals = []
        result_entries = []
        for entry in self.entries:
            pab_offset = entry.computed_pab_offset(len(self.pak))
            data = self.pab[pab_offset:pab_offset + entry.size]
            intervals.append((pab_offset, pab_offset + entry.size, entry.index))
            result_entries.append({
                "index": entry.index,
                "headerStart": entry.header_start,
                "type": hex8(entry.type),
                "fullName": hex8(entry.full_name),
                "nameSum": hex8(entry.name_sum),
                "rawStartField": entry.start,
                "rawSizeField": entry.size,
                "computedPabOffset": pab_offset,
                "computedPabEnd": pab_offset + entry.size,
                "gapBefore": len(entry.prepad),
                "flags": hex8(entry.flags),
                "unk": hex8(entry.unk),
                "sha256": sha256(data),
                "firstBytes": data[:32].hex(),
            })
        return {
            "pakPath": str(self.pak_path),
            "pabPath": str(self.pab_path),
            "pakSize": len(self.pak),
            "pabSize": len(self.pab),
            "entryCountIncludingLast": len(self.entries),
            "splitOffsetFormula": "pab_offset = header_start + raw_start_field - pak_size",
            "entries": result_entries,
            "tailSize": len(self.tail),
        }

    def build_pair(self, replacements: Dict[int, bytes] | None = None, preserve_original_tail_if_possible: bool = True) -> Tuple[bytes, bytes]:
        """Return (new_pak, new_pab). replacements are keyed by entry index."""
        replacements = replacements or {}
        pak_size = len(self.pak)
        new_pak = bytearray(self.pak)  # preserve header padding bytes exactly
        pab_parts: List[bytes] = []
        pab_offset = 0
        any_size_changed = False

        for entry in self.entries:
            data = replacements.get(entry.index, entry.data)
            if len(data) != entry.size:
                any_size_changed = True

            # Preserve the original gap before each entry. This makes no-edit roundtrip byte-identical
            # and preserves odd GH3 padding such as one-byte/five-byte gaps in MISC.
            if entry.prepad:
                pab_parts.append(entry.prepad)
                pab_offset += len(entry.prepad)

            entry.write_header_into(new_pak, pak_size, pab_offset, len(data))
            pab_parts.append(data)
            pab_offset += len(data)

        if preserve_original_tail_if_possible and not any_size_changed:
            pab_parts.append(self.tail)
        else:
            # PCtoConsole's preserved split output aligns the combined PAK+PAB stream, not the PAB alone.
            combined_len = pak_size + pab_offset
            pad_len = (-combined_len) % 0x1000
            if pad_len:
                pab_parts.append(bytes([0xAB]) * pad_len)

        return bytes(new_pak), b"".join(pab_parts)

    def write_pair(self, out_pak: Path, out_pab: Path, replacements: Dict[int, bytes] | None = None) -> None:
        new_pak, new_pab = self.build_pair(replacements)
        out_pak.parent.mkdir(parents=True, exist_ok=True)
        out_pab.parent.mkdir(parents=True, exist_ok=True)
        out_pak.write_bytes(new_pak)
        out_pab.write_bytes(new_pab)


@dataclasses.dataclass
class TexRecord:
    index: int
    record_offset: int
    raw_record: bytes
    checksum: int
    width: int
    height: int
    pass_field: int
    width2: int
    height2: int
    unk_a: int
    mip_count: int
    bpp: int
    unk_d: int
    end_offset: int
    chunk_offset: int
    size: int
    data_offset: int
    raw_chunk: bytes
    data: bytes

    @property
    def fourcc(self) -> str:
        img_type = self.raw_chunk[35] if len(self.raw_chunk) >= 36 else 0
        if img_type == 0x52:
            return "DXT1"
        if img_type in (0x53, 0x54):
            return "DXT5"
        if img_type == 0x71:
            return "ATI2"
        return "UNKNOWN"


class XboxTex:
    def __init__(self, data: bytes, records: List[TexRecord], meta_start: int, chunk_end: int):
        self.data = data
        self.records = records
        self.meta_start = meta_start
        self.chunk_end = chunk_end

    @classmethod
    def parse(cls, data: bytes) -> "XboxTex":
        if len(data) < 0x1C or u32be(data, 0) != TEX_MAGIC:
            raise ValueError("not a GH3 .tex.xen container")
        count = u16be(data, 6)
        meta_start = u32be(data, 8)
        chunk_end = u32be(data, 12)
        records: List[TexRecord] = []
        _ALL_TEX_OFFS = sorted({u32be(data[meta_start+j*TEX_META_RECORD_SIZE:meta_start+(j+1)*TEX_META_RECORD_SIZE],36) for j in range(count)})
        for i in range(count):
            off = meta_start + i * TEX_META_RECORD_SIZE
            raw = data[off:off + TEX_META_RECORD_SIZE]
            if len(raw) != TEX_META_RECORD_SIZE:
                raise ValueError(f"truncated TEX metadata record {i}")
            chunk_offset = u32be(raw, 28)
            size = u32be(raw, 32)
            data_offset = u32be(raw, 36)
            end_offset = u32be(raw, 24)
            if size == 0:
                size = next((o for o in _ALL_TEX_OFFS if o > data_offset), len(data)) - data_offset
            raw_chunk = data[chunk_offset:chunk_offset + TEX_CHUNK_RECORD_SIZE]
            payload = data[data_offset:data_offset + size]
            if len(raw_chunk) != TEX_CHUNK_RECORD_SIZE:
                raise ValueError(f"truncated TEX chunk record {i}")
            if len(payload) != size:
                raise ValueError(f"truncated TEX payload {i}")
            records.append(TexRecord(
                index=i,
                record_offset=off,
                raw_record=raw,
                checksum=u32be(raw, 4),
                width=u16be(raw, 8),
                height=u16be(raw, 10),
                pass_field=u16be(raw, 12),
                width2=u16be(raw, 14),
                height2=u16be(raw, 16),
                unk_a=u16be(raw, 18),
                mip_count=raw[20],
                bpp=raw[21],
                unk_d=u16be(raw, 22),
                end_offset=u32be(raw, 24),
                chunk_offset=chunk_offset,
                size=size,
                data_offset=data_offset,
                raw_chunk=raw_chunk,
                data=payload,
            ))
        return cls(data, records, meta_start, chunk_end)

    def by_checksum(self) -> Dict[int, TexRecord]:
        return {record.checksum: record for record in self.records}

    def report(self) -> dict:
        return {
            "texSize": len(self.data),
            "texSha256": sha256(self.data),
            "magic": hex8(u32be(self.data, 0)),
            "version": f"0x{u16be(self.data, 4):04x}",
            "count": len(self.records),
            "metaStart": self.meta_start,
            "chunkEnd": self.chunk_end,
            "records": [
                {
                    "index": r.index,
                    "checksum": hex8(r.checksum),
                    "width": r.width,
                    "height": r.height,
                    "width2": r.width2,
                    "height2": r.height2,
                    "pass": r.pass_field,
                    "unkA": r.unk_a,
                    "mips": r.mip_count,
                    "bpp": r.bpp,
                    "unkD": f"0x{r.unk_d:04x}",
                    "fourCC": r.fourcc,
                    "chunkOffset": r.chunk_offset,
                    "dataOffset": r.data_offset,
                    "size": r.size,
                    "endOffset": r.end_offset,
                    "imgType": f"0x{r.raw_chunk[35]:02x}",
                    "pitchByte": f"0x{r.raw_chunk[28]:02x}",
                    "dimensionField": hex8(u32be(r.raw_chunk, 36)),
                    "payloadSha256": sha256(r.data),
                }
                for r in self.records
            ],
        }


def dds_info(dds: bytes) -> dict:
    if len(dds) < DDS_HEADER_SIZE or dds[:4] != b"DDS ":
        raise ValueError("input is not a DDS file")
    fourcc = dds[84:88].decode("ascii", errors="replace").replace("\x00", "").strip()
    if fourcc not in FORMAT_INFO:
        raise ValueError(f"unsupported DDS FourCC {fourcc!r}; expected one of {sorted(FORMAT_INFO)}")
    return {
        "height": struct.unpack_from("<I", dds, 12)[0],
        "width": struct.unpack_from("<I", dds, 16)[0],
        "mip_count": struct.unpack_from("<I", dds, 28)[0] or 1,
        "fourcc": fourcc,
    }


def top_mip_size(width: int, height: int, fourcc: str) -> int:
    bpb = FORMAT_INFO[fourcc]["bytes_per_block"]
    return max(1, math.ceil(width / 4)) * max(1, math.ceil(height / 4)) * bpb


def xbox_storage_size(width: int, height: int, fourcc: str) -> int:
    bpb = FORMAT_INFO[fourcc]["bytes_per_block"]
    tiled_width = align(width, 128)
    tiled_height = align(height, 128)
    return (tiled_width // 4) * (tiled_height // 4) * bpb


def app_log2(n: int) -> int:
    r = -1
    while n:
        n >>= 1
        r += 1
    return r


def xbox360_tiled_offset(x: int, y: int, width_in_blocks: int, log_bpb: int) -> int:
    # Port of PCtoConsole TexHandler.GetXbox360TiledOffset.
    aligned_width = align(width_in_blocks, 32)
    macro = ((x >> 5) + (y >> 5) * (aligned_width >> 5)) << (log_bpb + 7)
    micro = ((x & 7) + ((y & 0xE) << 2)) << log_bpb
    offset = macro + ((micro & ~0xF) << 1) + (micro & 0xF) + ((y & 1) << 4)
    return (((offset & ~0x1FF) << 3)
            + ((y & 16) << 7)
            + ((offset & 0x1C0) << 2)
            + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6)
            + (offset & 0x3F)) >> log_bpb


def tile_xbox360_dxt(linear_pc_order: bytes, width: int, height: int, fourcc: str, *, word_swap: bool = True, base_data: bytes | None = None) -> bytes:
    bpb = FORMAT_INFO[fourcc]["bytes_per_block"]
    tiled_width = align(width, 128)
    tiled_height = align(height, 128)
    tiled_block_width = tiled_width // 4
    tiled_block_height = tiled_height // 4
    original_block_width = max(1, math.ceil(width / 4))
    original_block_height = max(1, math.ceil(height / 4))
    expected_linear = original_block_width * original_block_height * bpb
    if len(linear_pc_order) < expected_linear:
        raise ValueError(f"linear payload too small: have {len(linear_pc_order)}, need {expected_linear}")

    tiled_size = tiled_block_width * tiled_block_height * bpb
    out = bytearray(base_data[:tiled_size] if base_data and len(base_data) >= tiled_size else bytes(tiled_size))
    log_bpp = app_log2(bpb)

    # Mirror PCtoConsole's 16-pixel-wide correction.
    sx_offset = original_block_width if (tiled_block_width >= original_block_width * 2 and width == 16) else 0

    for dy in range(original_block_height):
        for dx in range(original_block_width):
            swz = xbox360_tiled_offset(dx + sx_offset, dy, tiled_block_width, log_bpp)
            sy, sx = divmod(swz, tiled_block_width)
            src = (dy * original_block_width + dx) * bpb
            dst = (sy * tiled_block_width + sx) * bpb
            block = linear_pc_order[src:src + bpb]
            if word_swap:
                for c in range(0, bpb, 2):
                    out[dst + c] = block[c + 1]
                    out[dst + c + 1] = block[c]
            else:
                out[dst:dst + bpb] = block
    return bytes(out)


def untile_xbox360_dxt(tiled: bytes, width: int, height: int, fourcc: str, *, word_swap: bool = True) -> bytes:
    """Exact inverse of tile_xbox360_dxt: Xbox-tiled payload -> linear PC-order
    top mip. Same sx_offset/word-swap so extract->replace round-trips."""
    bpb = FORMAT_INFO[fourcc]["bytes_per_block"]
    tiled_block_width = align(width, 128) // 4
    original_block_width = max(1, math.ceil(width / 4))
    original_block_height = max(1, math.ceil(height / 4))
    log_bpp = app_log2(bpb)
    sx_offset = original_block_width if (tiled_block_width >= original_block_width * 2 and width == 16) else 0
    out = bytearray(original_block_width * original_block_height * bpb)
    for dy in range(original_block_height):
        for dx in range(original_block_width):
            swz = xbox360_tiled_offset(dx + sx_offset, dy, tiled_block_width, log_bpp)
            sy, sx = divmod(swz, tiled_block_width)
            src = (sy * tiled_block_width + sx) * bpb
            dst = (dy * original_block_width + dx) * bpb
            block = tiled[src:src + bpb]
            if word_swap:
                for c in range(0, bpb, 2):
                    out[dst + c] = block[c + 1]
                    out[dst + c + 1] = block[c]
            else:
                out[dst:dst + bpb] = block
    return bytes(out)


def write_dds_bytes(width: int, height: int, fourcc: str, linear: bytes) -> bytes:
    """DDS file bytes for a single-mip DXT/ATI2 linear top mip."""
    bpb = FORMAT_INFO[fourcc]["bytes_per_block"]
    linear_size = max(1, math.ceil(width / 4)) * max(1, math.ceil(height / 4)) * bpb
    hdr = bytearray(128)
    struct.pack_into("<I", hdr, 0, 0x20534444)   # 'DDS '
    struct.pack_into("<I", hdr, 4, 124)
    struct.pack_into("<I", hdr, 8, 0x000A1007)   # flags: caps|height|width|pitch|pixelformat
    struct.pack_into("<I", hdr, 12, height)
    struct.pack_into("<I", hdr, 16, width)
    struct.pack_into("<I", hdr, 20, linear_size)
    struct.pack_into("<I", hdr, 24, 1)
    struct.pack_into("<I", hdr, 28, 1)           # mip count
    struct.pack_into("<I", hdr, 76, 32)          # pixelformat size
    struct.pack_into("<I", hdr, 80, 4)           # DDPF_FOURCC
    hdr[84:88] = fourcc.encode("ascii")
    struct.pack_into("<I", hdr, 108, 0x1000)     # DDSCAPS_TEXTURE
    return bytes(hdr) + linear[:linear_size]


def texture_dimension_field(width: int, height: int) -> int:
    return (((height - 1) << 13) | (width - 1)) & 0xFFFFFFFF


def texture_pitch_byte(width: int) -> int:
    return 0x80 | max(1, align(width, 128) // 128)


def make_meta_record(key: int, width: int, height: int, fourcc: str, chunk_offset: int, data_offset: int, size: int, template: bytes | None = None) -> bytes:
    rec = bytearray(template[:TEX_META_RECORD_SIZE] if template and len(template) >= TEX_META_RECORD_SIZE else bytes(TEX_META_RECORD_SIZE))
    info = FORMAT_INFO[fourcc]
    put_u16be(rec, 0, 0x0A28)
    rec[2] = 0x02
    # rec[3] is usually 0 for regular in-memory textures; preserve template if provided.
    put_u32be(rec, 4, key)
    put_u16be(rec, 8, width)
    put_u16be(rec, 10, height)
    # Preserve pass/unkA from template when available. Without a template, use the common replacement value 1.
    if not template:
        put_u16be(rec, 12, 1)
    put_u16be(rec, 14, width)
    put_u16be(rec, 16, height)
    if not template:
        put_u16be(rec, 18, 1)
    rec[20] = 1
    rec[21] = info["bpp"]
    # chunk[0x23] is the reliable format indicator. The metadata word at
    # +0x16 is not a FourCC selector in retail GH3 global_gfx: DXT5 entries
    # commonly use 0x0100. Preserve it from an Xbox template when available.
    if not template:
        put_u16be(rec, 22, info["unkD"])
    put_u32be(rec, 24, data_offset + size)
    put_u32be(rec, 28, chunk_offset)
    put_u32be(rec, 32, size)
    put_u32be(rec, 36, data_offset)
    return bytes(rec)


def make_chunk_record(width: int, height: int, fourcc: str, template: bytes | None = None) -> bytes:
    chunk = bytearray(template[:TEX_CHUNK_RECORD_SIZE] if template and len(template) >= TEX_CHUNK_RECORD_SIZE else bytes(TEX_CHUNK_RECORD_SIZE))
    if not template:
        put_u32be(chunk, 0, 0x00200003)
        put_u32be(chunk, 4, 0x00000001)
        put_u32be(chunk, 20, 0xFFFF0000)
        put_u32be(chunk, 24, 0xFFFF0000)
        put_u32be(chunk, 40, 0x00000D10)
        put_u32be(chunk, 48, 0x00000A00)
    chunk[28] = texture_pitch_byte(width)
    chunk[35] = FORMAT_INFO[fourcc]["img_type"]
    put_u32be(chunk, 36, texture_dimension_field(width, height))
    put_u32be(chunk, 44, 0)
    return bytes(chunk)


def scan_dds_folder(dds_dir: Path) -> List[Path]:
    files = [p for p in dds_dir.iterdir() if p.is_file() and p.name.lower().endswith(".dds") and p.stem.lower().startswith("0x")]
    return sorted(files, key=lambda p: p.name.lower())


def rebuild_tex_from_dds_folder(
    dds_dir: Path,
    *,
    template_tex: Optional[XboxTex] = None,
    stock_global_tex: Optional[XboxTex] = None,
    no_word_swap_keys: Iterable[int] = (),
    use_stock_payload_keys: Iterable[int] = (),
    meta_start: int = 0x0C38,
) -> Tuple[bytes, dict]:
    files = scan_dds_folder(dds_dir)
    if not files:
        raise ValueError(f"no 0x........dds files found in {dds_dir}")

    no_swap = set(no_word_swap_keys)
    stock_payload = set(use_stock_payload_keys)
    stock_by_key = stock_global_tex.by_checksum() if stock_global_tex else {}
    template_by_key = template_tex.by_checksum() if template_tex else {}

    # Metadata templates by shape. Prefer stock global: it contains the
    # retail Xbox texture-header style. This matters for open-note/test
    # textures that exist only in MISC, because preserving a previously
    # generated MISC record can carry forward bad pass/unkA/unkD flags.
    stock_template_by_shape: Dict[Tuple[int, int, str, int], bytes] = {}
    if stock_global_tex:
        for record in stock_global_tex.records:
            shape = (record.width, record.height, record.fourcc, record.mip_count)
            stock_template_by_shape.setdefault(shape, record.raw_record)

    # Chunk templates by FourCC. Prefer stock_global because it contains many known-good Xbox chunks.
    chunk_template_by_fourcc: Dict[str, bytes] = {}
    for source in [stock_global_tex, template_tex]:
        if not source:
            continue
        for record in source.records:
            if record.fourcc in FORMAT_INFO and record.fourcc not in chunk_template_by_fourcc:
                chunk_template_by_fourcc[record.fourcc] = record.raw_chunk
    count = len(files)
    chunk_start = meta_start + count * TEX_META_RECORD_SIZE
    chunk_end = chunk_start + count * TEX_CHUNK_RECORD_SIZE
    data_start = align(chunk_end, 0x1000)

    header = bytearray(bytes([0xEF]) * meta_start)
    put_u32be(header, 0, TEX_MAGIC)
    put_u16be(header, 4, TEX_VERSION)
    put_u16be(header, 6, count)
    put_u32be(header, 8, meta_start)
    put_u32be(header, 12, chunk_end)
    put_u32be(header, 16, 0xFFFFFFFF)
    # For a stock-like 0xC38 header, these two fields match the included MISC/global samples for 34/116 textures.
    header[20:24] = (template_tex.data[20:24] if template_tex and len(template_tex.data) >= 24 else b"\x00\x00\x00\x07")
    put_u32be(header, 24, 28)

    meta = bytearray(count * TEX_META_RECORD_SIZE)
    chunks = bytearray(count * TEX_CHUNK_RECORD_SIZE)
    payload_parts: List[bytes] = []
    cursor = data_start
    report = []

    for i, dds_path in enumerate(files):
        key = parse_hex_key(dds_path.stem)
        dds = dds_path.read_bytes()
        info = dds_info(dds)
        width, height, fourcc = info["width"], info["height"], info["fourcc"]
        linear_size = top_mip_size(width, height, fourcc)
        linear = dds[DDS_HEADER_SIZE:DDS_HEADER_SIZE + linear_size]
        if len(linear) != linear_size:
            raise ValueError(f"DDS top mip is truncated for {dds_path}")

        stock_record = stock_by_key.get(key)
        dds_mip_count = info.get("mip_count", 1)
        shape_template = stock_template_by_shape.get((width, height, fourcc, dds_mip_count))
        template_record = stock_record or template_by_key.get(key)
        base_data = stock_record.data if stock_record and len(stock_record.data) >= xbox_storage_size(width, height, fourcc) else None

        if key in stock_payload and stock_record:
            payload = stock_record.data[:xbox_storage_size(width, height, fourcc)]
            used_stock_payload = True
            word_swap = False
        else:
            word_swap = key not in no_swap
            payload = tile_xbox360_dxt(linear, width, height, fourcc, word_swap=word_swap, base_data=base_data)
            used_stock_payload = False

        # Align each texture payload start to 0x1000, matching the current builder and samples.
        pad_before = cursor - (data_start + sum(len(p) for p in payload_parts))
        if pad_before > 0:
            payload_parts.append(bytes(pad_before))
        data_offset = cursor
        payload_parts.append(payload)

        record_template = (stock_record.raw_record if stock_record and stock_record.width == width and stock_record.height == height else None) or shape_template or (template_record.raw_record if template_record and template_record.width == width and template_record.height == height else None)
        chunk_template = chunk_template_by_fourcc.get(fourcc)
        rec = make_meta_record(key, width, height, fourcc, chunk_start + i * TEX_CHUNK_RECORD_SIZE, data_offset, len(payload), record_template)
        ch = make_chunk_record(width, height, fourcc, chunk_template)
        meta[i * TEX_META_RECORD_SIZE:(i + 1) * TEX_META_RECORD_SIZE] = rec
        chunks[i * TEX_CHUNK_RECORD_SIZE:(i + 1) * TEX_CHUNK_RECORD_SIZE] = ch

        report.append({
            "checksum": hex8(key),
            "source": str(dds_path),
            "width": width,
            "height": height,
            "fourCC": fourcc,
            "dataOffset": data_offset,
            "size": len(payload),
            "payloadSha256": sha256(payload),
            "sourcePayloadSha256": sha256(linear),
            "wordSwap": word_swap,
            "usedStockPayload": used_stock_payload,
        })

        cursor += len(payload)
        cursor = align(cursor, 0x1000)

    pre_data = bytes(header) + bytes(meta) + bytes(chunks)
    if len(pre_data) > data_start:
        raise ValueError("metadata/chunk area exceeded data_start")
    tex_data = pre_data + bytes(data_start - len(pre_data)) + b"".join(payload_parts)
    return tex_data, {"texSize": len(tex_data), "texSha256": sha256(tex_data), "textures": report}



def extract_entry_data(pak_path: Path, pab_path: Path, *, index: Optional[int] = None, type: Optional[int] = None, full_name: Optional[int] = None) -> Tuple[PakEntry, bytes]:
    pair = SplitPak.read(pak_path, pab_path)
    if index is not None:
        entry = pair.entries[index]
    else:
        entry = pair.find_entry(type=type, full_name=full_name)
    return entry, entry.data


def extract_global_gfx_tex_from_pair(pak_path: Path, pab_path: Path) -> bytes:
    return extract_entry_data(pak_path, pab_path, type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)[1]


def command_inspect(args: argparse.Namespace) -> None:
    pak = SplitPak.read(Path(args.pak), Path(args.pab))
    print(json.dumps(pak.inspection(), indent=2))


def command_tex_report(args: argparse.Namespace) -> None:
    tex = XboxTex.parse(Path(args.tex).read_bytes())
    print(json.dumps(tex.report(), indent=2))



def command_extract_entry(args: argparse.Namespace) -> None:
    index = int(args.index) if args.index is not None else None
    type_value = parse_hex_key(args.type) if args.type else None
    full_name = parse_hex_key(args.full_name) if args.full_name else None
    entry, data = extract_entry_data(Path(args.pak), Path(args.pab), index=index, type=type_value, full_name=full_name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(json.dumps({
        "entryIndex": entry.index,
        "type": hex8(entry.type),
        "fullName": hex8(entry.full_name),
        "size": len(data),
        "sha256": sha256(data),
        "out": str(out),
    }, indent=2))


def command_roundtrip(args: argparse.Namespace) -> None:
    source = SplitPak.read(Path(args.pak), Path(args.pab))
    out_pak = Path(args.out_pak)
    out_pab = Path(args.out_pab)
    source.write_pair(out_pak, out_pab)
    print(json.dumps({
        "outPak": str(out_pak),
        "outPab": str(out_pab),
        "pakByteIdentical": source.pak == out_pak.read_bytes(),
        "pabByteIdentical": source.pab == out_pab.read_bytes(),
        "outPakSha256": sha256(out_pak.read_bytes()),
        "outPabSha256": sha256(out_pab.read_bytes()),
    }, indent=2))


def command_replace_global_gfx(args: argparse.Namespace) -> None:
    source = SplitPak.read(Path(args.misc_pak), Path(args.misc_pab))
    entry = source.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)
    tex_data = Path(args.global_gfx_tex).read_bytes()
    # Validate before writing.
    XboxTex.parse(tex_data)
    out_pak = Path(args.out_pak)
    out_pab = Path(args.out_pab)
    source.write_pair(out_pak, out_pab, {entry.index: tex_data})
    rebuilt = SplitPak.read(out_pak, out_pab)
    rebuilt_entry = rebuilt.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)
    print(json.dumps({
        "replacedEntryIndex": entry.index,
        "oldSize": entry.size,
        "newSize": len(tex_data),
        "oldSha256": sha256(entry.data),
        "newSha256": sha256(tex_data),
        "outPak": str(out_pak),
        "outPab": str(out_pab),
        "newComputedPabOffset": rebuilt_entry.computed_pab_offset(len(rebuilt.pak)),
        "outPakSha256": sha256(out_pak.read_bytes()),
        "outPabSha256": sha256(out_pab.read_bytes()),
    }, indent=2))


def command_build_tex(args: argparse.Namespace) -> None:
    template_tex = XboxTex.parse(Path(args.template_tex).read_bytes()) if args.template_tex else None
    stock_tex = XboxTex.parse(Path(args.stock_global_tex).read_bytes()) if args.stock_global_tex else None
    no_swap = [parse_hex_key(x) for x in (args.no_word_swap or [])]
    stock_payload = [parse_hex_key(x) for x in (args.use_stock_payload or [])]
    tex_data, report = rebuild_tex_from_dds_folder(
        Path(args.dds_dir),
        template_tex=template_tex,
        stock_global_tex=stock_tex,
        no_word_swap_keys=no_swap,
        use_stock_payload_keys=stock_payload,
        meta_start=parse_hex_key(args.meta_start) if isinstance(args.meta_start, str) and args.meta_start.lower().startswith("0x") else int(args.meta_start),
    )
    out = Path(args.out_tex)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(tex_data)
    report_path = Path(args.report) if args.report else out.with_suffix(out.suffix + ".report.json")
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps({"outTex": str(out), "report": str(report_path), **report}, indent=2))



def command_build_misc(args: argparse.Namespace) -> None:
    base_pair = SplitPak.read(Path(args.misc_pak), Path(args.misc_pab))
    base_entry = base_pair.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)
    template_tex = XboxTex.parse(base_entry.data)

    stock_tex = None
    if args.stock_global_pak and args.stock_global_pab:
        stock_tex = XboxTex.parse(extract_global_gfx_tex_from_pair(Path(args.stock_global_pak), Path(args.stock_global_pab)))

    no_swap = [parse_hex_key(x) for x in (args.no_word_swap or [])]
    stock_payload = [parse_hex_key(x) for x in (args.use_stock_payload or [])]
    tex_data, report = rebuild_tex_from_dds_folder(
        Path(args.dds_dir),
        template_tex=template_tex,
        stock_global_tex=stock_tex,
        no_word_swap_keys=no_swap,
        use_stock_payload_keys=stock_payload,
        meta_start=template_tex.meta_start,
    )

    out_pak = Path(args.out_pak)
    out_pab = Path(args.out_pab)
    base_pair.write_pair(out_pak, out_pab, {base_entry.index: tex_data})
    report_path = Path(args.report) if args.report else out_pak.with_suffix(out_pak.suffix + ".texture_report.json")
    report_path.write_text(json.dumps(report, indent=2))

    rebuilt = SplitPak.read(out_pak, out_pab)
    rebuilt_entry = rebuilt.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)
    print(json.dumps({
        "outPak": str(out_pak),
        "outPab": str(out_pab),
        "report": str(report_path),
        "globalGfxEntryIndex": base_entry.index,
        "globalGfxOldSize": base_entry.size,
        "globalGfxNewSize": len(tex_data),
        "globalGfxNewPabOffset": rebuilt_entry.computed_pab_offset(len(rebuilt.pak)),
        "texSha256": sha256(tex_data),
        "outPakSha256": sha256(out_pak.read_bytes()),
        "outPabSha256": sha256(out_pab.read_bytes()),
    }, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GH3 Xbox 360 split PAK/PAB + TEX starter builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="Inspect a split PAK/PAB pair")
    p.add_argument("pak")
    p.add_argument("pab")
    p.set_defaults(func=command_inspect)

    p = sub.add_parser("tex-report", help="Parse a .tex.xen and emit a JSON report")
    p.add_argument("tex")
    p.set_defaults(func=command_tex_report)

    p = sub.add_parser("extract-entry", help="Extract one split PAK/PAB entry")
    p.add_argument("pak")
    p.add_argument("pab")
    p.add_argument("out")
    p.add_argument("--index")
    p.add_argument("--type")
    p.add_argument("--full-name")
    p.set_defaults(func=command_extract_entry)

    p = sub.add_parser("roundtrip", help="Rewrite a split PAK/PAB pair with no edits; should be byte-identical")
    p.add_argument("pak")
    p.add_argument("pab")
    p.add_argument("out_pak")
    p.add_argument("out_pab")
    p.set_defaults(func=command_roundtrip)

    p = sub.add_parser("replace-global-gfx", help="Replace the global_gfx.tex entry in MISC")
    p.add_argument("misc_pak")
    p.add_argument("misc_pab")
    p.add_argument("global_gfx_tex")
    p.add_argument("out_pak")
    p.add_argument("out_pab")
    p.set_defaults(func=command_replace_global_gfx)

    p = sub.add_parser("build-tex", help="Build an Xbox .tex.xen from 0x........dds files")
    p.add_argument("dds_dir")
    p.add_argument("out_tex")
    p.add_argument("--template-tex")
    p.add_argument("--stock-global-tex")
    p.add_argument("--meta-start", default="0x0C38")
    p.add_argument("--no-word-swap", action="append", default=[], help="Texture checksum to copy without 16-bit swap. Repeatable.")
    p.add_argument("--use-stock-payload", action="append", default=[], help="Texture checksum to copy from stock global TEX. Repeatable.")
    p.add_argument("--report")
    p.set_defaults(func=command_build_tex)

    p = sub.add_parser("build-misc", help="Build MISC.pak.xen/MISC.pab.xen from a DDS folder by replacing global_gfx.tex")
    p.add_argument("misc_pak")
    p.add_argument("misc_pab")
    p.add_argument("dds_dir")
    p.add_argument("out_pak")
    p.add_argument("out_pab")
    p.add_argument("--stock-global-pak")
    p.add_argument("--stock-global-pab")
    p.add_argument("--no-word-swap", action="append", default=[], help="Texture checksum to copy without 16-bit swap. Repeatable.")
    p.add_argument("--use-stock-payload", action="append", default=[], help="Texture checksum to copy from stock global TEX. Repeatable.")
    p.add_argument("--report")
    p.set_defaults(func=command_build_misc)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
