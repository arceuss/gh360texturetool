#!/usr/bin/env python3
"""Manifest-driven GH3 Xbox 360 MISC split PAK/PAB builder.

This is the new project converter entry point. It is based on the audited
ChatGPT handoff implementation, but requires explicit per-texture manifest
choices instead of hidden environment variables.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

# Reuse the audited single-file implementation for the low-level binary work.
# Keep this wrapper small and explicit: it owns policy/manifest validation.
from tools.x360pak._consult_single import (
    SplitPak,
    XboxTex,
    TYPE_TEX,
    GLOBAL_GFX_TEX_FULLNAME,
    parse_hex_key,
    rebuild_tex_from_dds_folder,
    extract_global_gfx_tex_from_pair,
    sha256,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "openwork/misc/manifest.json"


def hex8(v: int) -> str:
    return f"0x{v & 0xFFFFFFFF:08X}"


def load_manifest(path: Path | None) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {"textures": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    data.setdefault("textures", {})
    if not isinstance(data["textures"], dict):
        raise ValueError("manifest.textures must be an object")
    return data


def manifest_options(manifest: Dict[str, Any]) -> tuple[list[int], list[int], Dict[str, Any]]:
    no_word_swap: list[int] = []
    use_stock_payload: list[int] = []
    normalized: Dict[str, Any] = {}

    for raw_key, cfg in manifest.get("textures", {}).items():
        key = parse_hex_key(raw_key)
        key_s = hex8(key)
        if not isinstance(cfg, dict):
            raise ValueError(f"texture {key_s}: config must be an object")
        source_order = cfg.get("source_order", "pc_dds")
        if source_order not in ("pc_dds", "already_xbox_word_order_dds", "stock_xbox_payload"):
            raise ValueError(f"texture {key_s}: invalid source_order {source_order!r}")

        word_swap = cfg.get("word_swap")
        if word_swap is None:
            word_swap = source_order == "pc_dds"
        if not isinstance(word_swap, bool):
            raise ValueError(f"texture {key_s}: word_swap must be boolean")

        if source_order == "already_xbox_word_order_dds" and word_swap:
            raise ValueError(f"texture {key_s}: already_xbox_word_order_dds requires word_swap=false")
        if source_order == "pc_dds" and not word_swap:
            raise ValueError(f"texture {key_s}: pc_dds normally requires word_swap=true; set source_order=already_xbox_word_order_dds if intentional")
        if source_order == "stock_xbox_payload":
            use_stock_payload.append(key)
        elif not word_swap:
            no_word_swap.append(key)

        normalized[key_s] = {
            "source_order": source_order,
            "word_swap": word_swap,
            "use_stock_payload": source_order == "stock_xbox_payload",
            "reason": cfg.get("reason", ""),
        }
    return no_word_swap, use_stock_payload, normalized


def validate_roundtrip(misc_pak: Path, misc_pab: Path, work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    out_pak = work_dir / "MISC.roundtrip.pak.xen"
    out_pab = work_dir / "MISC.roundtrip.pab.xen"
    pair = SplitPak.read(misc_pak, misc_pab)
    pair.write_pair(out_pak, out_pab, {})
    pak_ok = misc_pak.read_bytes() == out_pak.read_bytes()
    pab_ok = misc_pab.read_bytes() == out_pab.read_bytes()
    if not pak_ok or not pab_ok:
        raise SystemExit(
            "No-edit roundtrip failed; refusing to build. "
            f"pak_ok={pak_ok} pab_ok={pab_ok}"
        )


def command_roundtrip(args: argparse.Namespace) -> None:
    validate_roundtrip(Path(args.misc_pak), Path(args.misc_pab), Path(args.out_dir))
    print(json.dumps({"roundtrip": "ok", "outDir": args.out_dir}, indent=2))


def command_build(args: argparse.Namespace) -> None:
    misc_pak = Path(args.misc_pak)
    misc_pab = Path(args.misc_pab)
    dds_dir = Path(args.dds_dir)
    out_pak = Path(args.out_pak)
    out_pab = Path(args.out_pab)
    report_path = Path(args.report)
    manifest_path = Path(args.manifest) if args.manifest else DEFAULT_MANIFEST

    validate_roundtrip(misc_pak, misc_pab, Path(args.work_dir))

    manifest = load_manifest(manifest_path)
    no_swap, stock_payload, normalized_manifest = manifest_options(manifest)

    base_pair = SplitPak.read(misc_pak, misc_pab)
    base_entry = base_pair.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)
    template_tex = XboxTex.parse(base_entry.data)

    stock_tex = None
    if args.stock_global_pak and args.stock_global_pab:
        stock_tex = XboxTex.parse(extract_global_gfx_tex_from_pair(Path(args.stock_global_pak), Path(args.stock_global_pab)))

    tex_data, texture_report = rebuild_tex_from_dds_folder(
        dds_dir,
        template_tex=template_tex,
        stock_global_tex=stock_tex,
        no_word_swap_keys=no_swap,
        use_stock_payload_keys=stock_payload,
        meta_start=template_tex.meta_start,
    )

    out_pak.parent.mkdir(parents=True, exist_ok=True)
    out_pab.parent.mkdir(parents=True, exist_ok=True)
    base_pair.write_pair(out_pak, out_pab, {base_entry.index: tex_data})

    rebuilt = SplitPak.read(out_pak, out_pab)
    rebuilt_entry = rebuilt.find_entry(type=TYPE_TEX, full_name=GLOBAL_GFX_TEX_FULLNAME)

    report = {
        "builder": "tools/x360pak/manifest_misc.py",
        "manifestPath": str(manifest_path) if manifest_path.exists() else None,
        "normalizedManifest": normalized_manifest,
        "inputs": {
            "miscPak": str(misc_pak),
            "miscPab": str(misc_pab),
            "ddsDir": str(dds_dir),
            "stockGlobalPak": args.stock_global_pak,
            "stockGlobalPab": args.stock_global_pab,
        },
        "outputs": {
            "outPak": str(out_pak),
            "outPab": str(out_pab),
            "outPakSha256": sha256(out_pak.read_bytes()),
            "outPabSha256": sha256(out_pab.read_bytes()),
        },
        "globalGfx": {
            "entryIndex": base_entry.index,
            "oldSize": base_entry.size,
            "newSize": len(tex_data),
            "newPabOffset": rebuilt_entry.computed_pab_offset(len(rebuilt.pak)),
            "texSha256": sha256(tex_data),
        },
        "textureReport": texture_report,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps({"built": True, "report": str(report_path), **report["outputs"]}, indent=2))

    if args.install:
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_pak = backup_dir / f"MISC.pak.xen.bak-pre-manifest-misc-{stamp}"
        bak_pab = backup_dir / f"MISC.pab.xen.bak-pre-manifest-misc-{stamp}"
        shutil.copy2(misc_pak, bak_pak)
        shutil.copy2(misc_pab, bak_pab)
        shutil.copy2(out_pak, misc_pak)
        shutil.copy2(out_pab, misc_pab)
        print(json.dumps({"installed": True, "backupPak": str(bak_pak), "backupPab": str(bak_pab)}, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Manifest-driven GH3 Xbox MISC converter")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("roundtrip", help="Verify byte-identical no-edit split MISC roundtrip")
    p.add_argument("misc_pak")
    p.add_argument("misc_pab")
    p.add_argument("out_dir")
    p.set_defaults(func=command_roundtrip)

    p = sub.add_parser("build", help="Build MISC.pak.xen/MISC.pab.xen from DDS folder")
    p.add_argument("misc_pak")
    p.add_argument("misc_pab")
    p.add_argument("dds_dir")
    p.add_argument("out_pak")
    p.add_argument("out_pab")
    p.add_argument("--manifest", help="JSON manifest; default openwork/misc/manifest.json if present")
    p.add_argument("--stock-global-pak")
    p.add_argument("--stock-global-pab")
    p.add_argument("--work-dir", default="openwork/validator/manifest_misc")
    p.add_argument("--report", default="openwork/manifest_misc/build_report.json")
    p.add_argument("--install", action="store_true", help="Install over input MISC pair after writing backups")
    p.add_argument("--backup-dir", default="backups/open_note")
    p.set_defaults(func=command_build)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
