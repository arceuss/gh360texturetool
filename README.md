this is vibecoded because console is bullshit
## usage
```
py unpack.py <pak-or-folder> [out]     # Xbox .pak.xen | PS3 .PAK.PS3 | PC (decode-only)
   ...edit the PNGs in tex/ and img/ ...
py repack.py <that folder>             # writes <name>_new.* next to the originals
```
- **Xbox 360**: split `pak/pab`, auto-decompresses, tiled+word-swapped DXT handled.
- **PS3**: `NAME.PAK.PS3 [+ .PAB.PS3] + NAME_VRAM.PAK.PS3` triplet; pixels are plain
  linear DXT in the VRAM pak; COMPRESSED is unused on PS3 so edit `DATA/ZONES` as-is.
  Repack writes `NAME_new.PAK.PS3/.PAB.PS3/_VRAM.PAK.PS3` (only VRAM bytes change) —
  rename over the originals to install.
- **PC**: decode-only; copy PNGs into an Xbox/PS3 unpack folder (same names) and repack that.
- Only PNGs you actually edit get re-encoded; everything else stays byte-identical.
