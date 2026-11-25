"""Who has to approve a definition before agents may use it (the approval policy).

  - Ready-made templates (e.g. the GitHub engineering model) and imports an admin marks as certified (the
    definitions were already reviewed in dbt, Looker or our YAML): approved as declared.
  - Structure found in the data (entities, joins), synonyms and value lists: approved automatically once they
    pass the checks; an owner can retire them.
  - A new metric, or a changed formula of an approved one: draft until an owner or admin approves it.
  - Anything that failed a check: needs_review (hidden from agents).
An approved definition that did not change keeps its approval.
"""

AUTO_KINDS = ("entity", "join", "synonym", "value_list", "verified", "instructions")


def status_for(kind: str, *, source: str, certified: bool = False, declared: str | None = None,
               old: dict | None = None, new: dict | None = None, checks_ok: bool = True) -> str:
    if not checks_ok:
        return "needs_review"
    if old and old["status"] == "retired" and source in ("auto_draft", "connector"):
        return "retired"                                   # an owner retired it: drafting does not bring it back
    if old and old["status"] == "approved" and old["definition"] == new:
        return "approved"
    if source == "template" or (source == "import" and certified):
        return declared if declared in ("draft", "approved") else "approved"
    if kind in AUTO_KINDS:
        return "approved"
    return "draft"
