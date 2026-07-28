from __future__ import annotations


def wrap_with_provenance(fragment_id: str, text: str) -> str:
    """Wrap text in a provenance tag."""
    return f'<prov id="{fragment_id}">{text}</prov>'
