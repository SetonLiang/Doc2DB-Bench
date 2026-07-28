"""
Aggregate all table_id.evidence_*.json files in a folder into a single table_id.evidence.json.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from ..models import EvidenceFragment, EvidencePool


def _find_evidence_files(evidence_dir: Path) -> dict[str, list[Path]]:
    """Find all *.evidence_*.json files and group by table_id."""
    pattern = re.compile(r"^(.+)\.evidence_[^.]+\.json$")
    by_table: dict[str, list[Path]] = defaultdict(list)

    for f in evidence_dir.iterdir():
        if not f.is_file() or f.suffix != ".json":
            continue
        match = pattern.match(f.name)
        if match:
            table_id = match.group(1)
            by_table[table_id].append(f)

    return dict(by_table)


def _merge_fragments(pools: list[EvidencePool]) -> list[dict]:
    """Merge fragments from multiple pools, deduplicating by fragment_id (keep first)."""
    seen: set[str] = set()
    merged: list[dict] = []

    for pool in pools:
        for frag in pool.fragments:
            if frag.fragment_id not in seen:
                seen.add(frag.fragment_id)
                merged.append(frag.model_dump(mode="json"))

    return merged


def group_evidence(evidence_dir: Path) -> None:
    """Aggregate all table_id.evidence_*.json into table_id.evidence.json per table."""
    evidence_dir = Path(evidence_dir).resolve()
    if not evidence_dir.is_dir():
        raise NotADirectoryError(f"Evidence directory does not exist: {evidence_dir}")

    by_table = _find_evidence_files(evidence_dir)
    if not by_table:
        print(f"No *.evidence_*.json files found in {evidence_dir}")
        return

    for table_id, paths in sorted(by_table.items()):
        pools: list[EvidencePool] = []
        for p in sorted(paths):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                pool = EvidencePool.model_validate(data)
                pools.append(pool)
            except Exception as e:
                print(f"Warning: skip {p.name}: {e}")
                continue

        if not pools:
            continue

        merged = _merge_fragments(pools)
        fragments = [EvidenceFragment.model_validate(f) for f in merged]
        out_pool = EvidencePool(table_id=table_id, fragments=fragments)

        out_path = evidence_dir / f"{table_id}.evidence.json"
        out_path.write_text(
            json.dumps(out_pool.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Aggregated {len(paths)} file(s) -> {out_path.name} ({len(out_pool.fragments)} fragments)")


def main() -> None:
    # python src/utils/group_evidence.py data_construction/dataset/BIRD/ours/processed/cs_semester/tables/evidence
    parser = argparse.ArgumentParser(
        description="Aggregate table_id.evidence_*.json files into table_id.evidence.json"
    )
    parser.add_argument(
        "evidence_dir",
        type=Path,
        help="Path to the evidence folder containing *.evidence_*.json files",
    )
    args = parser.parse_args()
    group_evidence(args.evidence_dir)


if __name__ == "__main__":
    main()
