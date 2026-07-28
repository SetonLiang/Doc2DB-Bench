from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from .base import BaseAgent
from ..llm.base import BaseLLMClient
from ..models import ProfilerInput

PROFILER_SYSTEM_PROMPT = (
    "You are an expert Linguistic Profiler and Domain Analyst. "
    "Analyze real-world domain documents and produce a strict writing style guide in JSON."
)

PROFILER_PROMPT = """
[System Role]
You are an expert Linguistic Profiler and Document Architect. Your task is to deeply analyze the provided real-world document excerpts and extract its pure structural blueprint and grammatical rules, completely ignoring the actual semantic content.

[Source Excerpts]
{real_world_document_text}

[Task]
Analyze the text and output a JSON object containing the following keys:
1. "domain_label": A concise label identifying the industry/document type (e.g., "Clinical Healthcare - EMS PCR", "Finance - SEC Filing").
2. "macro_structure_skeleton": The physical and logical blueprint of the document. Do not describe the content; describe the spatial organization. How are sections divided? What does the visual hierarchy look like? 
   (e.g., "1. ALL-CAPS HEADER -> 2. Comma-separated metadata line -> 3. Dense chronological narrative paragraph -> 4. Tabular log of timestamps and procedures").
3. "micro_linguistic_mechanics": The exact sentence-level grammatical rules needed to replicate this style. You must detail:
   - Voice & Perspective: (e.g., strict passive voice, objective 3rd person).
   - Subject/Pronoun Rules: (e.g., dropped subjects/telegraphic style like 'Arrived at scene' instead of 'We arrived', strict avoidance of 'I/We').
   - Sentence Rhythm & Density: (e.g., short choppy action-oriented sentences vs. long complex clauses with multiple modifiers).
   - Jargon Integration Style: (How abbreviations/metrics are naturally embedded into sentences without explanation).

You MUST output exactly one valid JSON object and nothing else.
Required output schema:
{{
  "domain_label": "string",
  "macro_format": "string",
  "micro_syntax": "string"
}}
"""


PROFILER_PROMPT = """
[System Role]
You are an expert Document Layout Architect. Your only task is to analyze the provided real-world document excerpts and extract its strict "Visual Layout" and "Structural Flow", completely ignoring the actual domain content or specific vocabulary. 

[Source Excerpts]
{real_world_document_text}

[Task]
Analyze the text and output a JSON object to serve as a formatting template for a generative model. The goal is to make the generated text "look exactly like" the source visually and structurally, without forcing specific content.

Extract the following keys:
1. "domain_label": A concise label identifying the document type.
2. "structural_flow_blueprint": What is the sequence of information blocks? (e.g., "1. Introductory metadata paragraph -> 2. Categorized checklist -> 3. Tabular event log"). DO NOT use any specific names or facts from the text in your description.
3. "visual_layout_rules": The exact typographical and formatting rules. How are headers styled? (e.g., ALL CAPS). How are key-value pairs separated? (e.g., 'CATEGORY- Value'). Are there specific tabular layouts or paragraph spacings?
4. "syntactic_rhythm": The pure sentence-level pacing, grammar, and voice. 
   CRITICAL CONSTRAINT: You MUST NOT include ANY specific vocabulary, acronyms, jargon, or content examples from the source text. Describe the *mechanics* only. 
   (CORRECT: "Telegraphic, fragmented sentences, omitted subjects, passive voice, short and choppy"). 
   (WRONG: "Uses abbreviations like Pt, Hx, or clinical shorthand").

You MUST output exactly one valid JSON object and nothing else. Do not include introductory text.
Required output schema:
{{
  "domain_label": "string",
  "structural_flow_blueprint": "string",
  "visual_layout_rules": "string",
  "syntactic_rhythm": "string"
}}
"""


class ReferenceGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_label: str = ""
    structural_flow: str = ""
    visual_layout: str = ""
    syntactic_rhythm: str = ""
    reference_macro_format: str = ""
    reference_micro_syntax: str = ""
    reference_lexicon: str = ""
    reference_snippet: str = ""


class ProfilerAgent(BaseAgent[ProfilerInput, ReferenceGuide]):
    def __init__(self, llm: BaseLLMClient | None = None) -> None:
        self.llm = llm

    @staticmethod
    def _truncate_source(text: str, max_chars: int) -> str:
        cleaned = text.strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip()

    @staticmethod
    def _extract_json_block(text: str) -> str:
        raw = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            return raw[first : last + 1].strip()
        return raw

    @staticmethod
    def _normalize_lexicon(value: object) -> str:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(items)
        if isinstance(value, str):
            return value.strip()
        return ""

    def _build_prompt(self, input: ProfilerInput) -> str:
        source = self._truncate_source(input.real_world_document_text, input.max_source_chars)
        return PROFILER_PROMPT.format(real_world_document_text=source)

    def _fallback(self, input: ProfilerInput) -> ReferenceGuide:
        snippet = self._truncate_source(input.real_world_document_text, 600)
        return ReferenceGuide(
            domain_label=input.domain_label.strip(),
            reference_macro_format="Unable to parse model output; manual profiling needed.",
            reference_micro_syntax="Unable to parse model output; manual profiling needed.",
            reference_lexicon="",
            reference_snippet=snippet,
        )

    def run(self, input: ProfilerInput) -> ReferenceGuide:
        if self.llm is None:
            return self._fallback(input)

        response = self.llm.generate(
            self._build_prompt(input),
            system_prompt=PROFILER_SYSTEM_PROMPT,
            task="writing",
        ).strip()
        if not response:
            return self._fallback(input)

        try:
            payload = json.loads(self._extract_json_block(response))
        except json.JSONDecodeError:
            return self._fallback(input)

        parsed_domain_label = str(payload.get("domain_label", "")).strip()
        macro_format = str(payload.get("macro_format", "")).strip()
        micro_syntax = str(payload.get("micro_syntax", "")).strip()
        reference_lexicon = self._normalize_lexicon(payload.get("lexicon", ""))
        narrative_template = str(payload.get("narrative_template", "")).strip()

        structural_flow = str(payload.get("structural_flow_blueprint", "")).strip()
        visual_layout = str(payload.get("visual_layout_rules", "")).strip()
        syntactic_rhythm = str(payload.get("syntactic_rhythm", "")).strip()

        return ReferenceGuide(
            domain_label=parsed_domain_label or input.domain_label.strip(),
            # reference_macro_format=macro_format,
            # reference_micro_syntax=micro_syntax,
            structural_flow=structural_flow,
            visual_layout=visual_layout,
            syntactic_rhythm=syntactic_rhythm,

        )
