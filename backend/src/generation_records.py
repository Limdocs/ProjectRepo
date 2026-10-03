"""Quiz-generation status: the health of one quiz-generation attempt.

A generation record lives in the `question_sets` table under a `set_id`
derived from its `generation_id`, and becomes the question set itself when the
generation commits. Its `generation_status` is the only source of truth for
whether a request is active, superseded, failed, or complete:

    PENDING / GENERATING -> READY

or:

    PENDING / GENERATING -> FAILED (failure_code) | ABORTED | REVOKED

Source documents are not part of this state machine.
"""

from uuid import UUID, uuid5

GENERATION_NAMESPACE = uuid5(
    UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), "limdocs-quiz-generation"
)

ACTIVE_STATUSES = ("PENDING", "GENERATING")


def generation_set_id(generation_id):
    return str(uuid5(GENERATION_NAMESPACE, generation_id))


def generation_state(row):
    """Collapse a stored generation row into (GENERATING|READY|FAILED, code)."""
    status = str(row.get("generation_status") or "").strip().upper()
    if status == "READY":
        return "READY", None
    if status in ACTIVE_STATUSES:
        return "GENERATING", None
    if status == "REVOKED":
        return "FAILED", "GENERATION_SUPERSEDED"
    failure_code = str(row.get("failure_code") or "").strip()
    return "FAILED", failure_code or "INTERNAL_ERROR"
