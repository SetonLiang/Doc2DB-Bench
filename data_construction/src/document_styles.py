from __future__ import annotations

from enum import Enum


class StyleDomain(str, Enum):
    MEDICAL = "medical"
    FINANCE = "finance"
    LEGAL = "legal"
    COMPLIANCE = "compliance"
    INCIDENT = "incident"


class DocumentStyle(str, Enum):
    # medical domain
    MEDICAL_REPORT = "medical_report"
    MEDICAL_LITERATURE = "medical_literature"
    MEDICAL_CASE_NOTE = "medical_case_note"

    # finance domain
    FINANCE_REPORT = "finance_report"
    FINANCE_FILING = "finance_filing"
    FINANCE_MARKET_COMMENTARY = "finance_market_commentary"

    # legal domain
    LEGAL_CONTRACT = "legal_contract"
    LEGAL_MEMO = "legal_memo"
    LEGAL_OPINION = "legal_opinion"

    # compliance domain
    COMPLIANCE_MEMO = "compliance_memo"
    COMPLIANCE_POLICY = "compliance_policy"
    COMPLIANCE_AUDIT_REPORT = "compliance_audit_report"

    # incident domain
    INCIDENT_REVIEW = "incident_review"
    INCIDENT_POSTMORTEM = "incident_postmortem"
    INCIDENT_TIMELINE = "incident_timeline"


DOMAIN_STYLE_CATEGORIES: dict[StyleDomain, dict[str, DocumentStyle]] = {
    StyleDomain.MEDICAL: {
        "report": DocumentStyle.MEDICAL_REPORT,
        "literature": DocumentStyle.MEDICAL_LITERATURE,
        "case_note": DocumentStyle.MEDICAL_CASE_NOTE,
    },
    StyleDomain.FINANCE: {
        "report": DocumentStyle.FINANCE_REPORT,
        "filing": DocumentStyle.FINANCE_FILING,
        "market_commentary": DocumentStyle.FINANCE_MARKET_COMMENTARY,
    },
    StyleDomain.LEGAL: {
        "contract": DocumentStyle.LEGAL_CONTRACT,
        "memo": DocumentStyle.LEGAL_MEMO,
        "opinion": DocumentStyle.LEGAL_OPINION,
    },
    StyleDomain.COMPLIANCE: {
        "memo": DocumentStyle.COMPLIANCE_MEMO,
        "policy": DocumentStyle.COMPLIANCE_POLICY,
        "audit_report": DocumentStyle.COMPLIANCE_AUDIT_REPORT,
    },
    StyleDomain.INCIDENT: {
        "review": DocumentStyle.INCIDENT_REVIEW,
        "postmortem": DocumentStyle.INCIDENT_POSTMORTEM,
        "timeline": DocumentStyle.INCIDENT_TIMELINE,
    },
}
