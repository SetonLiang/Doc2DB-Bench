from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from .base import BaseAgent
from ..models import EvidenceFragment, SerializerInput, TaskBlock, TaskItem, TaskQueue


class SerializerAgent(BaseAgent[SerializerInput, TaskQueue]):
    def __init__(self) -> None:
        pass

    @staticmethod
    def _normalize_strategy_name(raw_strategy: Any) -> str:
        if hasattr(raw_strategy, "value"):
            strategy_name = str(raw_strategy.value)
        else:
            strategy_name = str(raw_strategy)

        aliases = {
            "entity_major": "entity_major",
            "row_major": "entity_major",
            "attribute_major": "attribute_major",
            "col_major": "attribute_major",
            "column_major": "attribute_major",
            "interleaving": "interleaving",
            "hard": "interleaving",
            "chaos": "chaos",
            "random": "chaos",
            "random_hard": "chaos",
        }
        return aliases.get(strategy_name.lower(), "entity_major")

    @staticmethod
    def _raw_strategy_name(raw_strategy: Any) -> str:
        if hasattr(raw_strategy, "value"):
            return str(raw_strategy.value).lower()
        return str(raw_strategy).lower()

    @staticmethod
    def _extract_ijk(fragment_id: str, fallback_row_index: int) -> tuple[int, int, int]:
        # Expected format: cell_r{i}_c{j}_frag{k}
        try:
            row_part = fragment_id.split("_r", maxsplit=1)[1]
            i_part, after_i = row_part.split("_c", maxsplit=1)
            j_part, k_part = after_i.split("_frag", maxsplit=1)
            return int(i_part), int(j_part), int(k_part)
        except (IndexError, ValueError):
            return fallback_row_index, 0, 0

    def _priority_vector(self, fragment_id: str, fallback_row_index: int, strategy: str) -> tuple[float, ...]:
        i, j, k = self._extract_ijk(fragment_id, fallback_row_index)
        epsilon = random.random()

        if strategy == "attribute_major":
            return float(j), float(i), float(k), epsilon
        if strategy == "interleaving":
            return float(j), float(k), float(i), epsilon
        if strategy == "chaos":
            return float(k), random.random(), random.random(), random.random()
        return float(i), float(j), float(k), epsilon

    @staticmethod
    def _cell_key_from_frag(frag: EvidenceFragment) -> tuple[int, int, str]:
        i, j, _ = SerializerAgent._extract_ijk(
            fragment_id=frag.fragment_id,
            fallback_row_index=frag.source_cell.row_index,
        )
        return i, j, str(frag.source_cell.column_name)

    @staticmethod
    def _cell_key_from_item(item: TaskItem) -> tuple[int, int, str]:
        i, j, _ = SerializerAgent._extract_ijk(
            fragment_id=item.fragment_id,
            fallback_row_index=item.source_cell.row_index,
        )
        return i, j, str(item.source_cell.column_name)

    @staticmethod
    def _coalesce_cell_groups(
        sorted_groups: list[list[EvidenceFragment]],
    ) -> list[list[EvidenceFragment]]:
        """Re-order text-groups so all groups belonging to the same cell are consecutive.
        The relative order of cells is determined by their first appearance in sorted_groups."""
        cell_first_pos: dict[tuple[int, int, str], int] = {}
        for idx, group in enumerate(sorted_groups):
            for frag in group:
                key = SerializerAgent._cell_key_from_frag(frag)
                if key not in cell_first_pos:
                    cell_first_pos[key] = idx

        def group_sort_key(item: tuple[int, list[EvidenceFragment]]) -> tuple[int, int]:
            orig_idx, group = item
            min_cell_first = min(
                cell_first_pos[SerializerAgent._cell_key_from_frag(f)] for f in group
            )
            return min_cell_first, orig_idx

        reordered = sorted(enumerate(sorted_groups), key=group_sort_key)
        return [g for _, g in reordered]

    @staticmethod
    def _chaos_ordered_groups(
        groups: list[list[EvidenceFragment]],
    ) -> list[list[EvidenceFragment]]:
        """CHAOS strategy: randomly shuffle cell order; within each cell keep
        text-groups sorted by their minimum fragment index k (ascending).
        Fragments within a text-group remain consecutive."""
        cell_to_groups: dict[tuple[int, int, str], list[tuple[int, list[EvidenceFragment]]]] = defaultdict(list)
        for group in groups:
            rep_frag = group[0]
            cell_key = SerializerAgent._cell_key_from_frag(rep_frag)
            min_k = min(
                SerializerAgent._extract_ijk(f.fragment_id, f.source_cell.row_index)[2]
                for f in group
            )
            cell_to_groups[cell_key].append((min_k, group))

        cells = list(cell_to_groups.keys())
        random.shuffle(cells)

        result: list[list[EvidenceFragment]] = []
        for cell_key in cells:
            for _, group in sorted(cell_to_groups[cell_key], key=lambda t: t[0]):
                result.append(group)
        return result

    def run(self, input: SerializerInput) -> TaskQueue:
        """Step 3: order and chunk evidence pool into serialized task blocks."""
        raw_strategy_name = self._raw_strategy_name(input.config.strategy.ordering_strategy)
        chunk_size = input.config.strategy.chunk_size

        # Step 1: group by identical text (same-text rule for all strategies).
        grouped_fragments_by_text: dict[str, list[EvidenceFragment]] = defaultdict(list)
        for frag in input.evidence_pool.fragments:
            grouped_fragments_by_text[frag.text].append(frag)
        groups: list[list[EvidenceFragment]] = list(grouped_fragments_by_text.values())

        # Step 2: order text-groups according to strategy.
        if raw_strategy_name == "chaos":
            # Randomly shuffled cell order; within each cell text-groups are k-ascending.
            sorted_groups = self._chaos_ordered_groups(groups)
        else:
            # HARD uses col_major (attribute_major) as its deterministic cell ordering base;
            # other strategies use their own priority vector.
            strategy_name = (
                "attribute_major"
                if raw_strategy_name == "hard"
                else self._normalize_strategy_name(input.config.strategy.ordering_strategy)
            )
            sorted_groups = sorted(
                groups,
                key=lambda group: min(
                    self._priority_vector(
                        fragment_id=frag.fragment_id,
                        fallback_row_index=frag.source_cell.row_index,
                        strategy=strategy_name,
                    )
                    for frag in group
                ),
            )
            sorted_groups = self._coalesce_cell_groups(sorted_groups)

        # Step 3: pack into blocks, never splitting a cell across blocks.
        blocks: list[TaskBlock] = []
        current_items: list[TaskItem] = []

        def _flush_block() -> None:
            if raw_strategy_name == "hard":
                # Randomly interleave fragments from different cells while preserving
                # each cell's internal fragment order (frag1 < frag2 < frag3 ...).
                # Assign a random base priority per cell; sort key = (cell_priority, k).
                cell_priority: dict[tuple[int, int, str], float] = {}

                def _hard_sort_key(it: TaskItem) -> tuple[float, int]:
                    ck = SerializerAgent._cell_key_from_item(it)
                    if ck not in cell_priority:
                        cell_priority[ck] = random.random()
                    _, _, k = SerializerAgent._extract_ijk(it.fragment_id, it.source_cell.row_index)
                    return cell_priority[ck], k

                current_items.sort(key=_hard_sort_key)
            block_index = len(blocks)
            blocks.append(
                TaskBlock(
                    block_id=f"block_{block_index}",
                    block_index=block_index,
                    items=list(current_items),
                )
            )
            current_items.clear()

        for group in sorted_groups:
            group_items = [
                TaskItem(
                    fragment_id=frag.fragment_id,
                    source_cell=frag.source_cell,
                    text=frag.text,
                )
                for frag in group
            ]

            # Never cut between two groups that share a cell —
            # even if it means the block exceeds chunk_size.
            shares_cell = False
            if current_items:
                current_cell_keys = {self._cell_key_from_item(it) for it in current_items}
                group_cell_keys = {self._cell_key_from_frag(f) for f in group}
                shares_cell = bool(current_cell_keys & group_cell_keys)

            if current_items and len(current_items) + len(group_items) > chunk_size and not shares_cell:
                _flush_block()

            current_items.extend(group_items)

        if current_items:
            _flush_block()

        return TaskQueue(strategy=input.config.strategy.ordering_strategy, blocks=blocks)
