"""Small DDS helpers for GH3 Xbox texture conversion.

DDS headers are not written into GH3 .tex.xen payloads; the converter consumes
only dimensions, FourCC, mip count, and the top-mip block data. These helpers
make input DDS files deterministic so header differences from texconv/extractors
cannot hide real payload differences.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from pathlib import Path

DDS_HEADER_SIZE = 128
DDS_MAGIC = b"DDS "
DDSD_CAPS = 0x00000001
DDSD_HEIGHT = 0x00000002
DDSD_WIDTH = 0x00000004
DDSD_PITCH = 0x00000008
DDSD_PIXELFORMAT = 0x00001000
DDSD_MIPMAPCOUNT = 0x00020000
DDSD_LINEARSIZE = 0x00080000
DDSD_DEPTH = 0x00800000
DDPF_FOURCC = 0x00000004
DDSCAPS_TEXTURE = 0x00001000

FOURCC_BPB = {
    "DXT1": 8,
    "DXT3": 16,
    "DXT5": 16,
    "ATI2": 16,
}


@dataclasses.dataclass(frozen=True)
class DdsInfo:
    width: int
    height: int
    fourcc: str
    mip_count: int
    pitch_or_linear: int
    flags: int
    caps: int
    payload_offset: int = DDS_HEADER_SIZE

    @property
    def bytes_per_block(self) -> int:
        return FOURCC_BPB[self.fourcc]

    @property
    def top_mip_size(self) -> int:
        return top_mip_size(self.width, self.height, self.fourcc)


def top_mip_size(width: int, height: int, fourcc: str) -> int:
    bpb = FOURCC_BPB[fourcc]
    return max(1, math.ceil(width / 4)) * max(1, math.ceil(height / 4)) * bpb


def parse_dds(data: bytes) -> DdsInfo:
    if len(data) < DDS_HEADER_SIZE or data[:4] != DDS_MAGIC:
        raise ValueError("not a DDS file")
    header_size = struct.unpack_from("<I", data, 4)[0]
    pf_size = struct.unpack_from("<I", data, 76)[0]
    if header_size != 124 or pf_size != 32:
        raise ValueError(f"unsupported DDS header sizes: header={header_size} pf={pf_size}")
    fourcc = data[84:88].decode("ascii", errors="replace").replace("\x00", "").strip()
    if fourcc not in FOURCC_BPB:
        raise ValueError(f"unsupported DDS FourCC {fourcc!r}")
    return DdsInfo(
        width=struct.unpack_from("<I", data, 16)[0],
        height=struct.unpack_from("<I", data, 12)[0],
        pitch_or_linear=struct.unpack_from("<I", data, 20)[0],
        mip_count=struct.unpack_from("<I", data, 28)[0] or 1,
        flags=struct.unpack_from("<I", data, 8)[0],
        fourcc=fourcc,
        caps=struct.unpack_from("<I", data, 108)[0],
    )


def canonical_header(width: int, height: int, fourcc: str, mip_count: int = 1) -> bytes:
    if fourcc not in FOURCC_BPB:
        raise ValueError(f"unsupported DDS FourCC {fourcc!r}")
    if mip_count != 1:
        raise ValueError("canonical GH3 Xbox builder DDS headers currently support one top mip")

    hdr = bytearray(DDS_HEADER_SIZE)
    hdr[0:4] = DDS_MAGIC
    struct.pack_into("<I", hdr, 4, 124)
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT
    struct.pack_into("<I", hdr, 8, flags)
    struct.pack_into("<I", hdr, 12, height)
    struct.pack_into("<I", hdr, 16, width)
    struct.pack_into("<I", hdr, 20, top_mip_size(width, height, fourcc))
    struct.pack_into("<I", hdr, 24, 0)  # depth unused for 2D compressed DDS
    struct.pack_into("<I", hdr, 28, mip_count)
    struct.pack_into("<I", hdr, 76, 32)
    struct.pack_into("<I", hdr, 80, DDPF_FOURCC)
    hdr[84:88] = fourcc.encode("ascii")
    struct.pack_into("<I", hdr, 108, DDSCAPS_TEXTURE)
    return bytes(hdr)


def normalize_dds_bytes(data: bytes) -> bytes:
    info = parse_dds(data)
    # The GH3 Xbox .tex builder consumes only the top mip. Normalize to a
    # one-mip DDS by preserving the top-mip payload and dropping any extra
    # mip bytes from the wrapper file.
    payload = data[info.payload_offset:info.payload_offset + info.top_mip_size]
    if len(payload) != info.top_mip_size:
        raise ValueError("DDS top mip is truncated")
    return canonical_header(info.width, info.height, info.fourcc, 1) + payload


def normalize_dds_file(src: Path, dst: Path) -> None:
    normalized = normalize_dds_bytes(src.read_bytes())
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(normalized)
