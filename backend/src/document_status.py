"""Document processing status: the health of one document's own ingestion.

`documents.processing_status` describes the document pipeline only:

    UPLOADED -> PROCESSING -> READY

or, when the document itself cannot be processed (unsupported type, S3 or
Textract failure, unusable content), `FAILED` / `ERROR`. Quiz generation is a
separate state machine that lives on the question-set generation record; it
never reads or writes this field.

Rows written before that separation can still carry quiz-generation state:
`GENERATING` (a quiz worker had claimed the document) or `FAILED` with a
`failure_code` (a quiz failure; only quiz generation ever wrote that field,
and document processing only ever wrote free-text `failure_reason`). Both
cases always have `s3_processed_key`, which quiz generation required and
document processing writes only on the way to `READY`, so those documents did
finish processing. They are reported as `READY` on read instead of being
rewritten, which keeps quiz code out of the document pipeline.
"""

READY = "READY"
FAILED_STATUSES = frozenset({"FAILED", "ERROR"})
_LEGACY_QUIZ_OWNED_STATUSES = frozenset({"GENERATING"})


def normalize_status(value):
    return str(value or "").strip().upper()


def effective_processing_status(item):
    """The document's own processing status, with legacy quiz state removed."""
    status = normalize_status(item.get("processing_status"))
    if not item.get("s3_processed_key"):
        return status
    if status in _LEGACY_QUIZ_OWNED_STATUSES:
        return READY
    if status in FAILED_STATUSES and item.get("failure_code"):
        return READY
    return status


def is_usable_quiz_source(item):
    """True when the document finished processing and its text is retrievable."""
    return bool(item.get("s3_processed_key")) and effective_processing_status(item) == READY
