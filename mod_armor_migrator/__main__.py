"""CLI entry point: `python -m mod_armor_migrator ...`"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .migrator import migrate_all, migrate_from_remap_json
from .padding import audit_empty_unit, builtin_template, extract_template


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_index = os.path.join(here, "archivehashes.json")

    p = argparse.ArgumentParser(
        prog="mod_armor_migrator",
        description=(
            "Replicate a Helldivers 2 armor mod across other armor archives by "
            "remapping FileIDs (Unit/Material/Texture/Bones) and material slot IDs."
        ),
    )
    p.add_argument("--patch", required=True,
                   help="Path to the mod patch file (e.g. /game/data/<hash>.patch_0). "
                        "The .gpu_resources and .stream siblings are loaded automatically.")
    p.add_argument("--out-dir", required=True,
                   help="Output directory; one subfolder per target armor will be created.")

    # Mode A: derive remap from game archives.
    p.add_argument("--data-dir", default=None,
                   help="Path to the game's `data/` directory containing armor archives. "
                        "REQUIRED unless --remap-json is given.")
    p.add_argument("--source", default=None,
                   help="Hex hash of the source armor archive. If omitted, auto-detect.")
    p.add_argument("--target", default=None,
                   help="Comma-separated target hashes (Mode A) or target NAMES (Mode B). "
                        "If omitted, migrate to every entry.")
    p.add_argument("--category", default="Armor",
                   help="Category key in archivehashes.json (default: Armor).")
    p.add_argument("--index", default=default_index,
                   help=f"Path to archivehashes.json (default: {default_index})")

    # Mode B: use a precomputed remap.json from extract_remap.py.
    p.add_argument("--remap-json", default=None,
                   help="Use a precomputed remap.json (from "
                        "`python -m mod_armor_migrator.extract_remap`) instead of "
                        "reading game archives. Targets are keyed by armor NAME.")
    p.add_argument("--reference-remap-json", default=None,
                   help="Mode A only: use a precomputed remap.json as a priority "
                        "override while still reading source/target archives from "
                        "--data-dir. Targets are keyed by armor NAME.")
    p.add_argument("--experimental-partial-remap", action="store_true",
                   help="Mode A only: write a diagnostic patch even when automatic "
                        "semantic remap is incomplete. Unmatched Unit entries are "
                        "skipped; unsafe non-Unit ordinal remaps are kept with "
                        "warnings. Intended for manual in-game testing.")

    p.add_argument("--empty-mesh-from", default=None,
                   help="Path to a patch file from which to extract the empty-Unit "
                        "template (smallest-GPU Unit). When target armors have more "
                        "Unit slots than the source covers (e.g. backpack mounts), "
                        "the extras are filled with this empty mesh so the original "
                        "armor parts are hidden. If omitted, the project-bundled "
                        "empty mesh template in `_builtin_empty_mesh.py` is used.")
    p.add_argument("--no-padding", action="store_true",
                   help="Disable empty-mesh padding even when extras are present.")
    p.add_argument("--empty-mesh-verbatim", action="store_true",
                   help="Write the empty-mesh template's TocData byte-for-byte "
                        "(only the top-level FileID changes). Use this when you "
                        "hand-authored a generic empty Unit and want zero internal "
                        "rewriting. Without this flag, the template is sanitized "
                        "to remove material refs / draw indices, then slot IDs are "
                        "remapped to the target armor's naming.")
    p.add_argument("--patch-suffix", default="patch_0",
                   help="Suffix to use for each output patch file (default: patch_0).")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v info, -vv debug.")
    args = p.parse_args(argv)

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(level=max(level, logging.DEBUG),
                        format="%(levelname)s %(message)s")

    target_list = None
    if args.target:
        target_list = [t.strip() for t in args.target.split(",") if t.strip()]

    template = None
    if not args.no_padding:
        try:
            if args.empty_mesh_from:
                template = extract_template(args.empty_mesh_from)
                logging.info("empty-mesh template (user): FileID=0x%016x, "
                             "toc=%d B, gpu=%d B (from %s)",
                             template.source_file_id, len(template.toc_data),
                             len(template.gpu_data), args.empty_mesh_from)
            else:
                template = builtin_template()
                logging.info("empty-mesh template: built-in sanitized source "
                             "(toc=%d B, gpu=%d B)",
                             len(template.toc_data), len(template.gpu_data))
            audit = audit_empty_unit(template.toc_data)
            if args.empty_mesh_verbatim and audit.external_refs:
                logging.warning("empty-mesh template has %d external reference(s); "
                                "verbatim mode will keep them",
                                len(audit.external_refs))
            if args.empty_mesh_verbatim and not audit.is_non_drawing:
                logging.warning("empty-mesh template still draws %d index/indices; "
                                "verbatim mode will keep them", audit.num_indices)
            if not args.empty_mesh_verbatim:
                logging.info("empty-mesh template will be sanitized before padding "
                             "(external refs=%d, draw indices=%d)",
                             len(audit.external_refs), audit.num_indices)
        except Exception as e:
            logging.warning("could not load empty-mesh template: %s", e)

    if args.remap_json:
        reports = migrate_from_remap_json(
            patch_path=args.patch,
            remap_json_path=args.remap_json,
            out_dir=args.out_dir,
            target_names=target_list,
            patch_suffix=args.patch_suffix,
            empty_unit_template=template,
            empty_mesh_verbatim=args.empty_mesh_verbatim,
        )
    else:
        if not args.data_dir:
            p.error("either --data-dir or --remap-json must be provided")
        reports = migrate_all(
            patch_path=args.patch,
            data_dir=args.data_dir,
            out_dir=args.out_dir,
            archive_index_json=args.index,
            source_hash=args.source,
            target_hashes=target_list,
            category=args.category,
            patch_suffix=args.patch_suffix,
            empty_unit_template=template,
            empty_mesh_verbatim=args.empty_mesh_verbatim,
            reference_remap_json=args.reference_remap_json,
            experimental_partial_remap=args.experimental_partial_remap,
        )
    print(f"\nDone. Wrote {len(reports)} variant(s) under {args.out_dir}")
    for r in reports:
        print(f"  {r.target.name:40s} -> {r.out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
