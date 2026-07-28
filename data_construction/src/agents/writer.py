from __future__ import annotations

import importlib.util
import json
import pprint
import random
import re
import threading
from pathlib import Path
from typing import Any

from .base import BaseAgent
from ..parameters import (
    build_complexity_protocol,
    build_hard_cases_protocol,
    build_noise_protocol,
    resolve_style_hint,
)
from ..llm.base import BaseLLMClient
from ..models import DocumentBlock, WriterInput

WRITER_SYSTEM_PROMPT = (
    "You are a document writing agent. "
    "Write fluent, coherent prose while strictly preserving tagged facts."
)

LEGACY_WRITER_BLOCK_PROMPT = """
[System Role & Objective]
You are an expert Writer Agent operating within a continuous, autoregressive document synthesis pipeline. 
Your objective is to generate ONE coherent paragraph block that seamlessly integrates structured ground-truth facts, while maintaining absolute narrative and logical continuity with the preceding text.

[Generation Context]
- Previous Document Context (CRITICAL Anchor): {previous_context}
- Current Block ID: {block_id} (Index: {block_index} of {total_blocks})

[Mandatory Ground Truth (Evidence)]
You MUST explicitly embed the following structured facts. Do NOT drop, skip, or alter any fact.
{facts_with_tags}

[Execution Instructions]
1. FACTUAL FIDELITY: You must use all provided mandatory facts. You may paraphrase for narrative fluency, but the underlying mathematical, logical, and semantic truth must remain 100% intact.
2. TAG PRESERVATION: Keep each fact strictly wrapped in its exact original structural tag format (e.g., `<rX_cY> ... </rX_cY>`).
3. STRICT CONTINUITY & STATE INHERITANCE (CRITICAL): You MUST maintain strict logical and semantic consistency with the [Previous Document Context]. 
   - Inherit all established rules, semantic mappings (e.g., if the previous context mapped a grade of 'B' to the word 'good', you must continue this mapping), timelines, and background settings.
   - Do NOT contradict established facts, and do NOT invent conflicting global variables.
4. SEAMLESS BRIDGING: Ensure the transition from the [Previous Document Context] into this new block feels like a continuous, cohesive, and natural document flow.
5. TITLE CONDITION: IF AND ONLY IF this is the very first block (`block_index == 0`), output a contextually appropriate Document Title as the first line. For all subsequent blocks (`block_index > 0`), do NOT output any titles or headers.
6. FORMATTING: Write in natural, fluent paragraph prose. Do NOT use bullet points, markdown formatting, or JSON. Output plain text paragraphs only.
7. FINAL OUTPUT: Return ONLY the final synthesized text for this block. Do not include conversational filler or meta-commentary.
"""

WRITER_BLOCK_PROMPT = """
[System Role & Objective]
You are an expert Writer Agent operating within a parameterized document synthesis pipeline. 
Your goal is to write ONE coherent, polished, and natural-sounding section of a larger document while perfectly preserving structured ground-truth facts.

[Generation Context]
- Previous Block Context (Tail Anchor): {previous_context}
- Current Block ID: {block_id} (Index: {block_index} of {total_blocks})

[Base Parameters]
- Global Target Document Length: {document_length_tokens} tokens
- Suggested Token Budget for THIS Block: {target_block_tokens} tokens
- Document Style & Tone: {document_style}
- Section Template Hint: {section_template}

[Injection Protocols]
- Hard Cases Protocol:
{hard_cases_protocol}
- Controlled Noise Protocol:
{noise_protocol}
- Representation Complexity Protocol:
{complexity_protocol}

[Mandatory Ground Truth (Evidence)]
You MUST explicitly embed the following structured facts. Do NOT drop any fact.
{facts_with_tags}

[Execution Instructions]
1. FACTUAL FIDELITY: Use all mandatory facts exactly once or more. You may paraphrase for fluency, but you MUST NOT alter the underlying factual meaning or logic.
2. TAG PRESERVATION: Keep each fact strictly wrapped in its original structural tag format (e.g., `<rX_cY> ... </rX_cY>`).
3. STYLISTIC ADAPTATION: Adapt your wording, vocabulary, and tone to perfectly match the requested [Document Style & Tone] and [Section Template Hint].
4. CONDITIONAL PROTOCOL COMPLIANCE: Review the Hard Cases, Noise, and Complexity protocols in the [Injection Protocols] block. If a protocol contains instructions, apply them strictly. If a protocol is empty, "None", or "N/A", safely ignore it.
5. LENGTH & PACING: Ensure this section is detailed and substantial enough to stay near the `{target_block_tokens}` suggested budget, while contributing to the global document length.
6. SEAMLESS BRIDGING: Ensure your text logically and tonally continues from the 'Previous Block Context' (if provided) without breaking the narrative flow.
7. FORMATTING: Write in natural paragraph prose. Do NOT use bullet points, markdown formatting, or JSON.
8. OUTPUT STRUCTURE: The absolute first line of your output MUST be the Section Title, immediately followed by the section body paragraphs.
9. FINAL OUTPUT: Return ONLY the final synthesized text. Do not include meta-commentary or conversational filler.
"""

# - Tone & Macro-Format: {reference_macro_format}
# - Micro-Syntax & Expressions: {reference_micro_syntax}
WRITER_BLOCK_PROMPT_REFERNENCE = """
[System Role & Objective]
You are an expert domain-specific writer operating as the Writer Agent in an iterative document synthesis pipeline. 
Your goal is to write ONE coherent and highly authentic section of a larger document, seamlessly camouflaging structured ground-truth data within natural, domain-specific prose.

[Generation Context]
- Previous Block Context (Tail Anchor): {previous_context}
- Current Block ID: {block_id} (Index: {block_index} of {total_blocks})

[Base Parameters]
- Global Target Document Length: {document_length_tokens} tokens
- Suggested Token Budget for THIS Block: {target_block_tokens} tokens
- Hard Cases Protocol:
{hard_cases_protocol}
- Controlled Noise Protocol:
{noise_protocol}
- Linguistic Complexity Protocol:
{complexity_protocol}

[Style Guide & Domain Alignment]
CRITICAL: You MUST strictly align your writing with the following extracted domain profile. 
- Domain Label: {reference_domain_label}
- Structural Flow Blueprint: {reference_structural_flow}
- Visual Layout: {reference_visual_layout}
- Syntactic Rhythm: {reference_syntactic_rhythm}

[Mandatory Ground Truth (Evidence)]
You MUST explicitly embed the following structured facts. Do NOT drop any fact.
{facts_with_tags}

[Execution Instructions]
1. FACTUAL FIDELITY: Use all mandatory facts exactly once or more. You may paraphrase for fluency, but you MUST NOT alter the underlying factual meaning or logic.
2. TAG PRESERVATION: Keep each fact strictly wrapped in its original structural tag format (e.g., `<rX_cY> ... </rX_cY>`).
3. DOMAIN CAMOUFLAGE (CRITICAL): Fully immerse the facts using the [Style Guide]. Employ the required jargon, syntax, and tone so the final text is indistinguishable from a genuine, professional document of this domain. HOWEVER, do not blindly copy hardcoded structural artifacts (e.g., specific section numbers, document IDs, or exact cross-references).
4. MACRO-STRUCTURE PACING (CRITICAL): Look at the 'Previous Block Context' to determine your current position within the 'Structural Flow Blueprint'. Do NOT attempt to generate the entire structural flow in this single block. Simply continue the formatting and narrative sequence naturally from where the previous text left off.
5. CONDITIONAL PROTOCOL COMPLIANCE: Review the Noise, Complexity, and Hard Cases protocols in [Base Parameters]. If a protocol contains instructions, apply them strictly to manipulate extraction difficulty. If a protocol is empty, "None", or "N/A", safely ignore that specific dimension and default to natural domain writing.
6. SEAMLESS BRIDGING: Ensure your text logically and tonally continues from the 'Previous Block Context' without breaking the narrative flow.
7. FORMATTING: Write in natural prose suitable for the target domain. Do NOT use markdown bullet points or JSON unless strictly required by the [Style Guide].
8. OUTPUT: Output the Section Title as the first line, immediately followed by the section body. Return ONLY the final synthesized text.
"""

class WriterAgent(BaseAgent[WriterInput, DocumentBlock]):
    def __init__(self, llm: BaseLLMClient | None = None) -> None:
        self.llm = llm
        self._database_root = ""
        # Key: config.py absolute path, Value: resolved unified style string.
        self._style_value_cache: dict[str, str] = {}
        self._style_cache_lock = threading.Lock()

    def set_database_root(self, database_root: str | None) -> None:
        self._database_root = (database_root or "").strip()

    @staticmethod
    def _use_parameterized_prompt(input: WriterInput) -> bool:
        return bool(input.config.base.use_injected_parameters)

    @staticmethod
    def _use_reference_prompt(input: WriterInput) -> bool:
        return bool(input.config.base.reference_guide_path.strip())

    @staticmethod
    def _load_reference_guide(input: WriterInput) -> dict[str, str]:
        path_text = input.config.base.reference_guide_path.strip()
        if not path_text:
            return {}
        guide_path = Path(path_text)
        if not guide_path.exists():
            return {}
        try:
            payload = json.loads(guide_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            "domain_label": str(payload.get("domain_label", "")).strip(),
            "reference_structural_flow": str(payload.get("structural_flow", "")).strip(),
            "reference_visual_layout": str(payload.get("visual_layout", "")).strip(),
            "reference_syntactic_rhythm": str(payload.get("syntactic_rhythm", "")).strip(),
            "reference_macro_format": str(payload.get("reference_macro_format", "")).strip(),
            "reference_micro_syntax": str(payload.get("reference_micro_syntax", "")).strip(),
            "reference_lexicon": str(payload.get("reference_lexicon", "")).strip(),
            "reference_snippet": str(payload.get("narrative_template", "")).strip(),
        }

    @staticmethod
    def _cell_tag_from_fragment_id(fragment_id: str) -> str:
        match = re.search(r"_r(\d+)_c(\d+)", fragment_id)
        if not match:
            return "r1_c1"
        row_idx, col_idx = match.groups()
        return f"r{row_idx}_c{col_idx}"

    @staticmethod
    def _merge_cell_tags(cell_tags: list[str]) -> str | None:
        parsed: list[tuple[int, int]] = []
        for tag in cell_tags:
            match = re.fullmatch(r"r(\d+)_c(\d+)", tag.strip())
            if not match:
                return None
            row_idx, col_idx = match.groups()
            parsed.append((int(row_idx), int(col_idx)))
        if not parsed:
            return None
        col_set = {col for _, col in parsed}
        if len(col_set) != 1:
            return None
        col = next(iter(col_set))
        rows = sorted({row for row, _ in parsed})
        row_part = ",".join(str(row) for row in rows)
        return f"r{row_part}_c{col}"

    def _build_tagged_facts(self, input: WriterInput) -> tuple[list[str], list[str]]:
        used_fragment_ids: list[str] = []
        tagged_entries: list[tuple[str, str]] = []
        for item in input.block.items:
            cell_tag = self._cell_tag_from_fragment_id(item.fragment_id)
            tagged_entries.append((cell_tag, item.text))
            used_fragment_ids.append(item.fragment_id)

        grouped_tags: dict[str, list[str]] = {}
        grouped_text: dict[str, str] = {}
        grouped_order: list[str] = []
        for cell_tag, text in tagged_entries:
            text_key = text.strip()
            if text_key not in grouped_tags:
                grouped_tags[text_key] = []
                grouped_text[text_key] = text
                grouped_order.append(text_key)
            if cell_tag not in grouped_tags[text_key]:
                grouped_tags[text_key].append(cell_tag)

        is_group_block = any(len(tags) > 1 for tags in grouped_tags.values())
        lines: list[str] = []
        if is_group_block:
            for idx, text_key in enumerate(grouped_order, start=1):
                merged_tags = grouped_tags[text_key]
                base_text = grouped_text[text_key]
                compact_tag = self._merge_cell_tags(merged_tags)
                if compact_tag is not None:
                    merged_facts = f"<{compact_tag}>{base_text}</{compact_tag}>"
                else:
                    merged_facts = " ".join(f"<{tag}>{base_text}</{tag}>" for tag in merged_tags)
                lines.append(f"{idx}. {merged_facts}")
        else:
            for idx, (cell_tag, text) in enumerate(tagged_entries, start=1):
                lines.append(f"{idx}. <{cell_tag}>{text}</{cell_tag}>")
        return lines, used_fragment_ids

    @staticmethod
    def _style_value_from_config(input: WriterInput) -> str:
        raw = getattr(input.config.base, "document_style", None)
        if hasattr(raw, "value"):
            return str(raw.value).strip()
        if raw is None:
            return ""
        return str(raw).strip()

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _variants_config_path(self) -> Path:
        db_root = self._database_root
        db_root_path = Path(db_root)
        if not db_root_path.is_absolute():
            db_root_path = self._project_root() / db_root_path
        return db_root_path / "config.py"

    @staticmethod
    def _config_cache_key(config_path: Path) -> str:
        try:
            return str(config_path.resolve())
        except OSError:
            return str(config_path)

    @staticmethod
    def _load_python_config_dict(config_path: Path) -> dict[str, Any]:
        if not config_path.exists():
            return {}
        spec = importlib.util.spec_from_file_location(
            f"data_construction_writer_config_{config_path.stem}",
            str(config_path),
        )
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = getattr(module, "CONFIG", None)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _save_python_config_dict(config_path: Path, payload: dict[str, Any]) -> None:
        rendered = pprint.pformat(payload, sort_dicts=False, width=100)
        config_path.write_text(f"CONFIG = {rendered}\n", encoding="utf-8")

    @staticmethod
    def _normalize_variants(payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        variants: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            style = str(item.get("style", "")).strip()
            description = str(item.get("description", "")).strip()
            if not style:
                continue
            variants.append({"style": style, "description": description})
        return variants

    @staticmethod
    def _compose_style_value(domain: str, style: str, description: str) -> str:
        return (
            f"domain: {domain.strip() or 'General'} | "
            f"style: {style.strip()} | "
            f"description: {description.strip()}"
        )

    @staticmethod
    def _variants_schema_path(config_path: Path) -> Path:
        return config_path.parent / "schema.json"

    def _infer_and_persist_variants(self, config_path: Path) -> list[dict[str, str]]:
        from ..utils.infer_domain import infer_domain_and_description

        schema_path = self._variants_schema_path(config_path)
        if not schema_path.exists():
            return []
        try:
            schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        try:
            inferred = infer_domain_and_description(schema_payload)
        except Exception:
            return []
        if not isinstance(inferred, dict):
            return []

        variants = self._normalize_variants(inferred.get("variants", []))
        if not variants:
            return []

        config = self._load_python_config_dict(config_path)
        if not config:
            config = {}
        config["variants"] = variants
        domain = str(inferred.get("domain", "")).strip()
        if domain:
            config["domain"] = domain
        try:
            self._save_python_config_dict(config_path, config)
        except OSError:
            return variants
        return variants

    def _resolve_style_with_variants(self, input: WriterInput) -> str:
        manual_style = self._style_value_from_config(input)
        if manual_style:
            # 手动指定 document_style 时，显式保留 style 名称，便于在 prompt 中标记。
            return f"{manual_style} | {resolve_style_hint(manual_style)}"

        config_path = self._variants_config_path()
        cache_key = self._config_cache_key(config_path)
        with self._style_cache_lock:
            cached = self._style_value_cache.get(cache_key)
        if cached:
            return cached

        config = self._load_python_config_dict(config_path)
        domain = str(config.get("domain", "")).strip() or "General"
        variants = self._normalize_variants(config.get("variants", []))

        if not variants:
            variants = self._infer_and_persist_variants(config_path)
            config = self._load_python_config_dict(config_path)
            domain = str(config.get("domain", "")).strip() or domain
        if variants:
            picked = random.choice(variants)
            resolved = self._compose_style_value(
                domain,
                str(picked.get("style", "")).strip(),
                str(picked.get("description", "")).strip(),
            )
            with self._style_cache_lock:
                self._style_value_cache.setdefault(cache_key, resolved)
                return self._style_value_cache[cache_key]
        resolved = self._compose_style_value("General", "medical_report", "")
        with self._style_cache_lock:
            self._style_value_cache.setdefault(cache_key, resolved)
            return self._style_value_cache[cache_key]


    def _target_block_tokens(self, input: WriterInput) -> int:
        total_tokens = input.config.base.document_length_tokens
        total_blocks = max(1, input.total_blocks)
        avg_budget = max(96, total_tokens // total_blocks)
        block_index = input.block.block_index
        swing = max(24, int(avg_budget * 0.2))
        pattern = (-swing, -swing // 2, 0, swing // 2, swing)
        offset = pattern[block_index % len(pattern)]
        return max(96, avg_budget + offset)

    @staticmethod
    def _truncate_previous_context(text: str, max_chars: int) -> str:
        """截断 previous context，保留末尾（最近）内容以控制 prompt 长度。
        在词边界处截断，避免出现不完整的 token/单词（如 'ttee'）。"""
        if not text or len(text) <= max_chars:
            return text
        truncated = text[-max_chars:]
        # 跳过开头的半截词，从第一个空白符后开始，保证 token 完整
        for i in range(min(len(truncated), 120)):
            if truncated[i] in " \n\t":
                truncated = truncated[i + 1 :].lstrip()
                break
        return truncated

    def _fallback_text(self, previous_context: str, facts_lines: list[str]) -> str:
        lines: list[str] = []
        if previous_context:
            lines.append(f"History Context: {previous_context}")
        lines.extend(line.split(". ", maxsplit=1)[1] if ". " in line else line for line in facts_lines)
        return "\n".join(lines)

    @staticmethod
    def _resolve_protocol_input(custom_value: str | int | None, auto_value: str) -> str:
        # None keeps legacy auto-generated behavior; empty string intentionally disables the protocol.
        if custom_value is None:
            return auto_value
        return custom_value.strip()

    @staticmethod
    def _format_protocol_block(protocol_text: str) -> str:
        """Render protocol as an indented multiline block for prompt readability."""
        text = (protocol_text or "").strip()
        if not text:
            return "    (none)"
        return "\n".join(f"    {line}" if line else "" for line in text.splitlines())

    def run(self, input: WriterInput) -> DocumentBlock:
        """Step 4: generate one polished document block with cell-level tags."""
        max_chars = input.config.runtime.max_previous_context_chars
        previous_context = self._truncate_previous_context(input.history.raw_text.strip(), max_chars)
        facts_lines, used_fragment_ids = self._build_tagged_facts(input)
        section_template = (
            input.config.base.section_templates[input.block.block_index]
            if input.block.block_index < len(input.config.base.section_templates)
            else "(none)"
        )
        noise_protocol = self._format_protocol_block(self._resolve_protocol_input(
            custom_value=input.config.base.noise_protocol,
            auto_value=build_noise_protocol(input.config.base.noise_level),
        ))
        complexity_protocol = self._format_protocol_block(self._resolve_protocol_input(
            custom_value=input.config.base.complexity_protocol,
            auto_value=build_complexity_protocol(input.config.base.linguistic_complexity),
        ))
        hard_cases_protocol = self._format_protocol_block(self._resolve_protocol_input(
            custom_value=input.config.base.hard_cases_protocol,
            auto_value=build_hard_cases_protocol(),
        ))
        facts_with_tags = "\n".join(facts_lines) if facts_lines else "(empty)"
        reference_guide = self._load_reference_guide(input)

        if self._use_reference_prompt(input):
            reference_domain_label = (
                reference_guide.get("domain_label", "").strip()
                or "General Domain"
            )
            reference_snippet = (
                reference_guide.get("reference_snippet", "").strip()
                or "No reference snippet available; imitate formal domain style conservatively."
            )
            reference_macro_format = (
                reference_guide.get("reference_macro_format", "")
                or "Follow the provided reference structure and section cadence."
            )
            reference_micro_syntax = (
                reference_guide.get("reference_micro_syntax", "")
                or "Mimic sentence patterns, punctuation style, and phrasing density."
            )
            reference_lexicon = (
                reference_guide.get("reference_lexicon", "")
                or "Reuse domain-specific terminology and expressions from the reference."
            )
            reference_structural_flow = (
                reference_guide.get("reference_structural_flow", "")
                or "Follow the provided reference structure and section cadence."
            )
            reference_visual_layout = (
                reference_guide.get("reference_visual_layout", "")
                or "Maintain domain-appropriate layout patterns, separators, and formatting conventions."
            )
            reference_syntactic_rhythm = (
                reference_guide.get("reference_syntactic_rhythm", "")
                or "Mimic sentence pacing, clause density, and voice from the reference."
            )
            prompt = WRITER_BLOCK_PROMPT_REFERNENCE.format(
                previous_context=previous_context or "(empty)",
                block_id=input.block.block_id,
                block_index=input.block.block_index,
                total_blocks=input.total_blocks,
                document_length_tokens=input.config.base.document_length_tokens,
                target_block_tokens=self._target_block_tokens(input),
                hard_cases_protocol=hard_cases_protocol,
                noise_protocol=noise_protocol,
                complexity_protocol=complexity_protocol,
                reference_domain_label=reference_domain_label,
                reference_structural_flow=reference_structural_flow,
                reference_visual_layout=reference_visual_layout,
                reference_syntactic_rhythm=reference_syntactic_rhythm,
                # reference_macro_format=reference_macro_format,
                # reference_micro_syntax=reference_micro_syntax,
                # reference_lexicon=reference_lexicon,
                # reference_snippet=reference_snippet,
                facts_with_tags=facts_with_tags,
            )
        elif self._use_parameterized_prompt(input):
            style_value = self._resolve_style_with_variants(input)
            prompt = WRITER_BLOCK_PROMPT.format(
                previous_context=previous_context or "(empty)",
                block_id=input.block.block_id,
                block_index=input.block.block_index,
                total_blocks=input.total_blocks,
                document_length_tokens=input.config.base.document_length_tokens,
                target_block_tokens=self._target_block_tokens(input),
                document_style=style_value,
                section_template=section_template,
                hard_cases_protocol=hard_cases_protocol,
                noise_protocol=noise_protocol,
                complexity_protocol=complexity_protocol,
                facts_with_tags=facts_with_tags,
            )
        else:
            prompt = LEGACY_WRITER_BLOCK_PROMPT.format(
                previous_context=previous_context or "(empty)",
                block_id=input.block.block_id,
                block_index=input.block.block_index,
                total_blocks=input.total_blocks,
                facts_with_tags=facts_with_tags,
            )
            # if "r42" in prompt:
            #     print(prompt)
            #     print("----")
        # print(prompt)
        # exit()

        text = ""
        if self.llm is not None:
            text = self.llm.generate(
                prompt,
                system_prompt=WRITER_SYSTEM_PROMPT,
                task="writing",
            ).strip()
        
        if not text:
            text = self._fallback_text(previous_context=previous_context, facts_lines=facts_lines)

        return DocumentBlock(
            block_id=input.block.block_id,
            block_index=input.block.block_index,
            text=text,
            used_fragment_ids=used_fragment_ids,
        )
