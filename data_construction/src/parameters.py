from __future__ import annotations

from .document_styles import DocumentStyle

COMPLEXITY_RULES = {
    0: "LITERAL TRANSPARENCY: strict SVO structure, denotative language, no idioms/metaphors.",
    25: "DECORATIVE DILUTION: mild adjectives/passive voice; slight entity-value separation.",
    50: "BUREAUCRATIC ABSTRACTION: abstract nouns and longer causal structures increase distance.",
    75: "SEMANTIC SUBMERGENCE: dense jargon, nominalization, and unrelated numerical clutter.",
    100: "COGNITIVE OBFUSCATION: double negation, hedging, metaphorical framing, ambiguous references.",
}

STYLE_GUIDANCE = {
    # Backward compatibility
    "audit_report": "Use objective and evidence-driven wording with professional risk/compliance tone.",
    # Domain-adaptive styles (keys are always DocumentStyle enum values).
    DocumentStyle.MEDICAL_REPORT.value: "Use objective clinical reporting style with precise findings and patient-safe phrasing.",
    DocumentStyle.MEDICAL_LITERATURE.value: "Use formal academic biomedical writing with citations-like tone and cautious claims.",
    DocumentStyle.MEDICAL_CASE_NOTE.value: "Use concise chart-note style with temporal progression and intervention details.",
    DocumentStyle.FINANCE_REPORT.value: "Use audit-ready financial narrative with metric-driven summaries and neutral tone.",
    DocumentStyle.FINANCE_FILING.value: "Use regulatory filing style, dense risk disclosures, and legal-financial precision.",
    DocumentStyle.FINANCE_MARKET_COMMENTARY.value: "Use analytical market language with trend framing and quantified observations.",
    DocumentStyle.LEGAL_CONTRACT.value: "Use formal legal phrasing, obligations/conditions language, and precise semantics.",
    DocumentStyle.LEGAL_MEMO.value: "Use legal-analytical memo style with issue-rule-application-conclusion structure.",
    DocumentStyle.LEGAL_OPINION.value: "Use authoritative legal opinion tone with structured reasoning and precedent framing.",
    DocumentStyle.COMPLIANCE_MEMO.value: "Use concise policy-oriented language with explicit controls and rationale.",
    DocumentStyle.COMPLIANCE_POLICY.value: "Use normative policy language with mandatory controls and enforceable clauses.",
    DocumentStyle.COMPLIANCE_AUDIT_REPORT.value: "Use objective and evidence-driven wording with professional risk/compliance tone.",
    DocumentStyle.INCIDENT_REVIEW.value: "Use chronological analytical narrative, emphasizing cause, impact, and resolution.",
    DocumentStyle.INCIDENT_POSTMORTEM.value: "Use root-cause and remediation framing with blameless but technically precise tone.",
    DocumentStyle.INCIDENT_TIMELINE.value: "Use time-anchored event sequencing with operational clarity and concise transitions.",
}


def to_percent(level: float) -> int:
    return max(0, min(100, int(round(level * 100))))


def resolve_style_hint(style_value: str) -> str:
    normalized = (style_value or "").strip()
    if not normalized:
        return "Use formal professional writing style."

    # 1) Direct match for enum value string (e.g. "medical_report")
    if normalized in STYLE_GUIDANCE:
        return STYLE_GUIDANCE[normalized]

    # 2) Support enum member name input (e.g. "MEDICAL_REPORT")
    upper_name = normalized.upper()
    if upper_name in DocumentStyle.__members__:
        enum_value = DocumentStyle[upper_name].value
        return STYLE_GUIDANCE.get(enum_value, "Use formal professional writing style.")

    # 3) Tolerate case differences for value-like strings
    lowered = normalized.lower()
    if lowered in STYLE_GUIDANCE:
        return STYLE_GUIDANCE[lowered]

    return "Use formal professional writing style."


def build_noise_protocol(noise_level: float) -> str:
    level = to_percent(noise_level)
    if level <= 0:
        return "Noise disabled (0%). Keep prose clean and fully task-relevant."

    sentences_interval = max(1, 100 // level)
    return (
        f"# Controlled Noise Injection (Level: {level}%)\n"
        f"- Task: Every {sentences_interval} sentences, inject one strategic distractor.\n"
        f"- Audit Requirement: Final sentence count should be about {(1 + level / 100):.2f}x data density.\n"
        "- Non-Schema Entity Enrichment:\n"
        "  - Add detailed but schema-irrelevant context around entities.\n"
        "  - Never overwrite or contradict core ground-truth attributes.\n"
        "  - Ensure smooth transitions between real facts and injected noise.\n"
        "- Core Rules:\n"
        "  - Maintain a professional tone with syntactic camouflage.\n"
        "  - Keep noise non-destructive and semantically coherent.\n"
        "  - Prioritize extraction difficulty without factual drift."
    )


def build_complexity_protocol(linguistic_complexity: float) -> str:
    recursive_level = to_percent(linguistic_complexity)
    levels = sorted(COMPLEXITY_RULES.keys())
    selected_level = min(levels, key=lambda x: abs(x - recursive_level))
    selected_instruction = COMPLEXITY_RULES[selected_level]
    return (
        f"# Representation Complexity (Recursive Level: {recursive_level}%)\n"
        f"- Complexity Protocol: {selected_instruction}\n"
        "- Challenge Goal: modulate entity-value proximity to match target extraction difficulty."
    )


def build_hard_cases_protocol() -> str:
    return (
        "# Strategic Stress-Testing Protocols (The Challenges):\n"
        "1. Entity Disambiguation (Linguistic Variation & Anaphora):\n"
        "   - DO NOT repeat the full name in every sentence.\n"
        "   - Utilize anaphoric references (e.g., he, she, the aforementioned stakeholder, the asset).\n"
        "   - Utilize name aliases when natural.\n"
        "2. Temporal Evolution (The Administrative Correction):\n"
        "   - Narrative Arc: Initial (Incorrect) Assertion -> Intermediate Flux -> Final (Ground Truth) Resolution.\n"
        "   - Mandatory Numerical Decoy:\n"
        "     - You are required to include one plausible intermediate value that differs from ground truth.\n"
        "     - Constraint: the decoy must keep similar format/magnitude but be numerically distinct.\n"
        "   - Procedural Rectification:\n"
        "     - Narrate transition in audit-style logic from decoy value to ground-truth value."
    )
