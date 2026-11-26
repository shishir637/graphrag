"""Which facts may go live. Step 1: confident rule matches are approved, the rest wait for an owner."""

AUTO_APPROVE = 0.8


def validate_document(document: str, kind: str, chunks: list[dict], facts: list[dict]) -> list[dict]:
    for f in facts:
        f["status"], f["reason"] = ("approved", "") if f["confidence"] >= AUTO_APPROVE else ("needs_review", "low confidence")
    return facts
