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

### mipmaps
Repack regenerates the **full mip chain** (all levels), so recolored notes don't bleed
the old colour at a distance. PS3 mips are plain linear; 360 mips are tiled with a
packed mip tail, handled by `bin/xgtool.exe` (built from the real Xbox 360 XGraphics
lib — standalone, no SDK needed to run). If `xgtool.exe` is missing, 360 repack falls
back to top-mip-only (older behaviour). Small "packed" textures whose whole chain lives
in one tile (many starpower/whammy overlays) are now extracted and editable too.

Each level is downscaled with Pillow **BOX** (area-average, matches the game's box
reduce) and DXT-compressed with `bin/texconv.exe` (DirectXTex — much cleaner blocks and
far less mip banding than Pillow's encoder). `texconv.exe` needs the VC++ 2015–2022
redistributable (not the 360 SDK; it's near-universal on Windows). If it's missing or
can't launch, repack automatically falls back to Pillow's DXT encoder — nothing breaks.

### larger / resized textures
Drop in a PNG at a **different size** than the original and repack keeps it — the game
reads dimensions from the texture header, so a higher-res sidebar/HUD element just works.
`img/` entries grow in place; records inside a `.tex` container (e.g. `sys_sidebar2D` in
global_gfx) get their new payload appended past the existing data and re-pointed, so every
other texture keeps its exact bytes (mips + aliasing intact). Resized textures are written
**single-mip** — fine for HUD/menu/sidebar art, which renders near 1:1 on screen. Any size
is honored, including **1×1** — a legit trick to hide an element (e.g. star-power flames);
the new size is shown in the repack log so an accidental shrink is still visible.

### sidebar 1:1 fix (PC -> 360)
The 360 engine draws the highway sidebar sprite on a different quad than GH3 PC
(lower, larger, wider), so a PC theme's sidebar renders ~20px inward and thicker on
console. `fix_sidebar_360.py` warps the texture through an empirically calibrated
field (measured with a calibration texture on real hardware + GH3 PC; validated to
~0.5px) so the 360 render matches PC pixel-for-pixel:
```
py fix_sidebar_360.py sys_sidebar2D.png            # writes sys_sidebar2D_360fix.png
py fix_sidebar_360.py --unpack <unpack folder>     # patch in place, then repack
```
Needs `sidebar_360_warp.npz` beside the script. Calibrated for the 400x512 sidebar
canvas (Rainbow Zones) at 720p.

### scn pivots (HUD element positions)
Where a HUD element sits on screen (e.g. the sidebar) lives in the `.scn` scene
file, not a texture. Unpack writes a `scn/<name>.json` per scene: each node listed with its
id, the textures it references, and its transform floats (position/scale) at their exact
byte offsets, at **full precision**. Edit a number and repack patches just that float back
in place (same size, no reflow) — untouched values round-trip bit-exact, so only real edits
apply. Endianness is auto-detected from the `0x0410` scene tag, so it works on **360, PS3,
and PC** (PC is decode-only, so its `scn/` is for reference). Example: node 9's `X` in
`global_gfx` moves the sidebar horizontally.

### real texture names
Unpack names PNGs from the bundled `texture_names.json` (a baked checksum→name map),
so you get `img/training_guitar.png` and `tex/global_gfx/sys_gem2d_yellow.png` instead
of hex. Nothing extra needed. Anything not in the map keeps its `0x<checksum>` name —
that name is still the repack key, so renaming/rebuilding always works. Custom textures
(e.g. MISC taps that never existed in vanilla) stay as checksums.

Regenerating the map (dev only — needs the reference game data + script library):
`py build_texture_names.py`. It merges each platform's `dbg.pak` (file/container
names) with the SCN material bridge (individual note/gem/HUD names, resolved via the
`neversoft-script-library` scripts). You can also point unpack at a live `dbg.pak`
(4th arg) or pass `-` to use the bundled map only.
