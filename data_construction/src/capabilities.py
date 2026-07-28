# A single, authoritative source for all capability definitions.
# Each capability includes detailed and short descriptions for prompts/rules.
# Pillar 1: Intra-Table (Entity); Pillar 2: Inter-Table (Relationship).

# Pillar 1: Intra-Table (Entity) capability keys
PILLAR1_KEYS = frozenset({
    "TA_FC", "TA_US", "TA_EM",
    "RI_AC", "RI_LD", "RI_TC", "RI_MI",
    "TD_AS", "TD_DF", "TD_CA",
    "EF_ND", "EF_HC", "EF_RC",
})

# Pillar 2: Inter-Table (Relationship) capability keys
PILLAR2_KEYS = frozenset({
    "RL_O2M", "RL_MB", "RL_CL",
    "IDR_ED", "IDR_CR",
    "GR_TI", "RL_MI", "GR_DC",
    "IC_ME", "IC_NR",
})

PILLAR2_KEYS_OLD = frozenset({
    "SN_ED", "SN_TN",
    "RL_IFK", "RL_CK",
    "IDR_GD", "IDR_CR",
    "GR_TI", "GR_CTA", "GR_DC",
    "IC_RI", "IC_NNP",
})


def get_capability_definitions(
    table_type: str | None,
    *,
    detailed: bool = False,
) -> dict[str, str]:
    """
    Return capability definitions filtered by table type.

    - entity (or None): all capabilities (Pillar 1 + Pillar 2)
    - relation: only Pillar 2 (Inter-Table) capabilities
    """
    source = DETAILED_CAPABILITY_DEFINITIONS if detailed else SHORT_CAPABILITY_DEFINITIONS
    if (table_type or "").strip().lower() == "relation":
        return {k: v for k, v in source.items() if k in PILLAR2_KEYS}
    return dict(source)



DETAILED_CAPABILITY_DEFINITIONS = {
    # Pillar 1: Intra-Table (Entity)
    "TA_FC": """* **TA_FC (Format Canonicalization):** Normalize heterogeneous textual formats into a canonical database representation. Example: "31st of March" -> DATE:2024-03-31.""",
    "TA_US": """* **TA_US (Unit Standardization):** Convert units/scales into comparable numeric standards without semantic loss. Example: "$5M" -> 5000000.""",
    "TA_EM": """* **TA_EM (Equivalent Mapping):** Map semantically related but non-equivalent concepts expressed in the document into an abstract or canonical schema-defined representation. Example: "silver medal" -> Rank 2.""",

    "RI_AC": """* **RI_AC (Arithmetic Calculation):** Derive in-row values via arithmetic operations (+, -, *, /) from available attributes. Example: Price x Qty -> Total.""",
    "RI_LD": """* **RI_LD (Logical Derivation):** Infer boolean or categorical targets from row-level conditions/rules. Example: Age < 18 -> Is_Minor=True.""",
    "RI_TC": """* **RI_TC (Temporal Calculation):** Perform date/time reasoning, including offsets and durations. Example: 2020 to 2022 -> Duration=2 years.""",
    "RI_MI": """* **RI_MI (Multi-hop Inference):** Resolve chained row-level reasoning paths (A->B->C) to compute final targets.""",
    
    "TD_AS": """* **TD_AS (Attribute Selection):** Select the correct target attribute among semantically close fields using schema context. Example: Document has 'Contract Date' and 'Signature Date'. Schema defines date as 'Date when signed'. Model must select the latter.""",
    "TD_DF": """* **TD_DF (Distractor Filtering):** Reject invalid, obsolete, negated, or explicitly corrected distractor values. Example: Document says 'Price is 100 no, 120'. Model must ignore 100.""",
    "TD_CA": """* **TD_CA (Conflict Arbitration):** Resolve conflicting values under precedence rules (e.g., latest wins, trusted source priority).""",

    "EF_ND": """* **EF_ND (Null Detection):** Return NULL when evidence is insufficient or required attributes are absent.""",
    "EF_HC": """* **EF_HC (Hallucination Check):** Prevent injection of external/world knowledge not grounded in evidence. Example: Must not automatically fill 'CEO: Tim Cook' just because 'Apple' is mentioned.""",
    "EF_RC": """* **EF_RC (Redundancy Control):** Merge repeated mentions that refer to the same entity/fact into one coherent record. Example: Text introduces 'Project A' at the beginning and mentions it again at the end. Extraction should yield only 1 Project record, not 2.""",

    # Pillar 2: Inter-Table (Relationship)
    "RL_MI": """* **RL_MI (Multi-hop Inference):** Establish a valid multi-dimensional relationship by performing multi-hop reading comprehension across scattered text spans to assemble all required foreign key attributes. Example: Hop 1 states 'Alice enrolled in the director's course.' Hop 2 states 'The director teaches CS-501.' Hop 3 states 'The syllabus was updated for Fall 2025.' The model must chain these three scattered hops to successfully generate the complete relation record (Alice, CS-501, Fall 2025).""",
    "RL_O2M": """* **RL_O2M (One-to-Many/Many-to-One Allocation):** Unpack a compressed natural language statement into multiple distinct relationship rows mapping cardinality (1:N or N:1). Example: Text states 'Manager Alice took over all three projects (X, Y, and Z) previously handled by Bob.' The model must generate three separate relation rows: (Alice, Project_X), (Alice, Project_Y), and (Alice, Project_Z), avoiding non-normalized array outputs.""",
    "RL_MB": """* **RL_MB (Multi-Entity Binding):** Correctly align N-ary relations (e.g., Supplier-Part-Project) from intertwined narratives without mismatching pairs. Example: Text says 'Supplier A delivered CPUs to Project X and GPUs to Project Y. Supplier B also delivered CPUs but to Project Z.' The model must generate exact triplets (A, CPU, X), (A, GPU, Y), and (B, CPU, Z) without hallucinating a (B, CPU, X) relation.""",
    "RL_CL": """* **RL_CL (Conditional Linkage):** Establish foreign key relations based on implicit business rules applied to entity attributes. Example: Text rule states 'All Senior-level engineers are automatically assigned to the Architecture Board.' The model must check the Employee table, find all engineers with the 'Senior' attribute, and automatically generate relation rows linking them to the Board, even if their names aren't explicitly mentioned in the text.""",

    "IDR_ED": """* **IDR_ED (Entity Disambiguation):** Resolve ambiguous aliases or mentions using contextual clues and table attributes. Example: Text mentions "Old Zhang solved the tech issue". The model must use the "tech" context to map "Old Zhang" to "Zhang Wei (ID:02, Tech Dept)" instead of "Zhang Wei (ID:01, Sales Dept)" in the Employee table.""",
    "IDR_CR": """* **IDR_CR (Coreference Resolution):** Resolve pronouns, role titles, or aliases to the correct entity using cross-sentence context. Example: Given 'Alice was appointed as CEO last quarter' and later 'The CEO approved it, and she forwarded it to the team lead', the model must resolve both 'The CEO' and 'she' to Alice and populate approver_id in the Approval table.""",

    "GR_TI": """* **GR_TI (Transitive Inference):** Infer transitive relations over linked records (A->B, B->C => A->C). Example: Text states "Alice leads the Mobile Team", and a table shows "Mobile Team handles Project_X". The model must deduce the implicit relation (Alice, Project_X).""",
    "GR_CTA": """* **GR_CTA (Cross-Table Aggregation):** Compute parent-level attribute values by aggregating (SUM, COUNT, AVG) data across multiple related child-table records along a 1:N foreign key path. Example: To fill the 'total_price' in the Orders table, the model must sum the 'price' of all associated items in the Order_Details child table instead of relying on a single direct mention.""",
    "GR_DC": """* **GR_DC (Dynamic Change):** Reason over cross-table event timelines, sequential relationships, or entity state changes to determine the final relationship status.. Example: Deducing from a narrative that Student 10 completed Course 13 before enrolling in Course 12.""",
    
    "IC_NR": """* **IC_NR (Null Relation Extraction):** The capability to correctly extract an empty relationship state (e.g., NULL) by parsing explicit negative evidence, such as cancelled or unfulfilled prerequisites. The model must suppress the urge to extract a relationship based on surface-level co-occurrence and strictly avoid hallucinating fabricated links.""",
    "IC_ME": """* **IC_ME (Mutual Exclusion):** Infer the strict boundaries of a relationship based on negative constraints or exceptions. Example: Text states 'The new security patch was deployed to all servers in the US-East region, except for the legacy database servers.' The model must generate relations for the US-East servers but explicitly drop/exclude relations for any server flagged as 'legacy' in the database."""
}


SHORT_CAPABILITY_DEFINITIONS = {
    "TA_FC": "TA_FC: Format Canonicalization - normalize text/date formats to canonical DB style.",
    "TA_US": "TA_US: Unit Standardization - standardize units/scales/currency into consistent values.",
    "TA_EM": "TA_EM: Equivalent Mapping - map semantically related documentary concepts to schema-defined representations.",
    "RI_AC": "RI_AC: Arithmetic Calculation - derive values via basic arithmetic in one row.",
    "RI_LD": "RI_LD: Logical Derivation - infer target values from row-level rules/conditions.",
    "RI_TC": "RI_TC: Temporal Calculation - compute time offsets/durations/date transforms.",
    "RI_MI": "RI_MI: Multi-hop Inference - chain multiple row-level reasoning steps.",
    "TD_AS": "TD_AS: Attribute Selection - choose the correct field among semantically similar columns.",
    "TD_DF": "TD_DF: Distractor Filtering - keep verified value, reject incorrect/obsolete distractors.",
    "TD_CA": "TD_CA: Conflict Arbitration - resolve contradictions using explicit precedence rules.",
    "EF_ND": "EF_ND: Null Detection - output NULL when evidence is insufficient.",
    "EF_HC": "EF_HC: Hallucination Check - avoid unsupported external knowledge.",
    "EF_RC": "EF_RC: Redundancy Control - merge duplicate mentions of same entity/fact.",

    "RL_MI": "RL_MI: Multi-hop Inference - chain scattered evidence across multiple paragraphs to assemble a complete relationship record.",
    "RL_O2M": "RL_O2M: One-to-Many Allocation - unpack aggregated text into multiple distinct 1:N or N:1 relation rows.",
    "RL_MB": "RL_MB: Multi-Entity Binding - accurately align triplets/N-ary relations in complex narratives.",
    "RL_CL": "RL_CL: Conditional Linkage - establish relations based on implicit business rules applied to entity attributes.",
    
    "IDR_ED": "IDR_ED: Entity Disambiguation - resolve ambiguous aliases using contextual clues.",
    "IDR_CR": "IDR_CR: Coreference Resolution - resolve pronouns and aliases across sentences/paragraphs to one stable identity.",
    
    "GR_TI": "GR_TI: Transitive Inference - deduce implicit relations via linked paths (A->B->C).",
    "GR_CTA": "GR_CTA: Cross-Table Aggregation - compute parent values by aggregating (e.g., SUM, COUNT) related child records.",
    "GR_DC": "GR_DC: Dynamic Change - determine final relation status from state/event sequences.",
    
    "IC_NR": "IC_NR: Referential Integrity - drop orphan records when a required parent foreign key is missing or unresolvable.",
    "IC_ME": "IC_ME: Mutual Exclusion - omit relation records based on negative constraints or listed exceptions."
}
