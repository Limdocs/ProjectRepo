import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4, uuid5

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from content_selection import (
    MAX_PASSAGES_PER_DOCUMENT,
    MIN_PASSAGE_CHARS,
    TARGET_PASSAGE_CHARS,
    DocumentRef,
    SelectionConfig,
    select_quiz_content_streaming,
)
from course_access import require_course_owner
from document_status import is_usable_quiz_source
from generation_records import (
    GENERATION_NAMESPACE as _GENERATION_NAMESPACE,
    generation_set_id as _generation_set_id,
)
from token_budget import (
    ContentSelectionError,
    count_overhead_tokens,
    get_token_counter,
    model_limits,
    reserved_output_tokens,
)
from topic_scoring import compute_topic_scores, select_prioritized_weak_topics
from openai_helpers import (
    FALLBACK_TOPICS,
    allowed_en_topic_names,
    build_canonical_topic_lookup,
    dedupe_topics_by_en,
    ensure_document_topics,
    extract_json_payload,
    get_openai_client,
    openai_config,
)

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
QUESTION_SETS_TABLE = os.environ["QUESTION_SETS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
USER_PROGRESS_TABLE = os.environ.get("USER_PROGRESS_TABLE", "")
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]

_ALLOWED_DIFFICULTIES = {"Easy", "Medium", "Hard"}
_ALLOWED_REQUESTED_QUESTION_COUNTS = {5, 10, 15, 20}
_ALLOWED_QUIZ_LANGUAGES = {"he", "en"}
_SHORT_SOURCE_HINT_THRESHOLD = 2000
_SHORT_SOURCE_HINT = (
    "Note: source text is limited; prioritize faithful coverage of the "
    "provided material over novelty."
)
_WORKER_TIMEOUT_SECONDS = 300
_DISPATCH_LEASE_SECONDS = _WORKER_TIMEOUT_SECONDS
_RUNNING_LEASE_SECONDS = 330
_OPENAI_TIMEOUT_SECONDS = 60
_MAX_TRANSACT_ACTIONS = 100

_CONTENT_SELECTION_CODE_MAP = {
    "FULL_TEXT_OVER_BUDGET": "CONTENT_FULL_TEXT_OVER_BUDGET",
    "INSUFFICIENT_PASSAGE_BUDGET": "CONTENT_INSUFFICIENT_PASSAGE_BUDGET",
    "ALL_SOURCES_EMPTY": "CONTENT_SOURCES_EMPTY",
    "NO_DOCUMENTS_SELECTED": "CONTENT_SOURCES_EMPTY",
    "BUDGET_NOT_CONVERGED": "CONTENT_SELECTION_FAILED",
    "UNSUPPORTED_MODEL": "CONTENT_SELECTION_FAILED",
    "TOKENIZER_UNAVAILABLE": "CONTENT_SELECTION_FAILED",
    "INVALID_BUDGET_CONFIGURATION": "CONTENT_SELECTION_FAILED",
    "CONFLICTING_DUPLICATE_DOCUMENT_ID": "CONTENT_SELECTION_FAILED",
    "INVALID_DOCUMENT_ID": "CONTENT_SELECTION_FAILED",
}


class GenerationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_dynamodb_transactions = boto3.client("dynamodb")
_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")
_documents_table = _dynamodb.Table(DOCUMENTS_TABLE)
_questions_table = _dynamodb.Table(QUESTIONS_TABLE)
_question_sets_table = _dynamodb.Table(QUESTION_SETS_TABLE)
_courses_table = _dynamodb.Table(COURSES_TABLE)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"

def _language_instruction_block(quiz_language):
    if quiz_language == "en":
        return (
            "LANGUAGE (STRICT):\n"
            "- Write every question, all four options, every explanation, and the answer "
            "string in English.\n"
        )
    return (
        "LANGUAGE (STRICT):\n"
        "- Write every question, all four options, every explanation, and the answer "
        "string in Hebrew.\n"
    )


def _build_weak_topic_priority_block(prioritized_weak_topics):
    topics_json = json.dumps(prioritized_weak_topics, ensure_ascii=False)
    return (
        "\n\nWEAK-TOPIC PRIORITY:\n"
        f"The learner is weaker in these topics: {topics_json}.\n"
        "- Target approximately 60–70% of questions on these topics when the source "
        "material supports it.\n"
        "- Remaining questions may cover other allowed topics from the selected documents.\n"
        "- All questions must remain grounded ONLY in the provided source text.\n"
        '- The "topics" field must still use exact allowed English names only.\n'
    )


def _build_system_prompt(
    allowed_topic_names,
    requested_question_count,
    quiz_language,
    prioritized_weak_topics=None,
):
    allowed_json = json.dumps(allowed_topic_names, ensure_ascii=False)
    language_block = _language_instruction_block(quiz_language)
    weak_block = ""
    if prioritized_weak_topics:
        weak_block = _build_weak_topic_priority_block(prioritized_weak_topics)
    return (
        "You are an expert academic assistant. Generate exactly "
        f"{requested_question_count} high-quality multiple-choice questions based on "
        "the provided text.\n\n"
        f"{language_block}\n"
        "TOPIC CONSTRAINT (STRICT — NO EXCEPTIONS):\n"
        f"The ONLY allowed topic names are: {allowed_json}.\n"
        "- You are STRICTLY FORBIDDEN from inventing new topic names, translating "
        "topic names, abbreviating them, or introducing typos.\n"
        '- Every question MUST include a "topics" array containing one or more values '
        "copied EXACTLY from the allowed list above (character-for-character match).\n"
        '- Do not use Hebrew topic names in the "topics" field — English names only.\n'
        "- Choose topics that best reflect the question content; a question may have "
        "multiple topics if appropriate.\n\n"
        "SOURCE FIDELITY (STRICT):\n"
        "- Every question, all four options, and every explanation must be grounded ONLY "
        "in the provided source text.\n"
        "- Do NOT invent facts, names, dates, definitions, or scenarios not supported by "
        "the source.\n"
        f"- You MUST return exactly {requested_question_count} questions.\n"
        "- If the source material is limited or thin relative to the requested count:\n"
        "  - Still return exactly the requested number of questions.\n"
        "  - Prefer rephrasing, combining, comparing, and testing understanding of the "
        "same concepts rather than inventing new content.\n"
        "  - Vary angle and difficulty on the same facts; use cross-document synthesis "
        "when multiple documents are present.\n"
        "  - Do NOT fill gaps with generic or plausible-sounding but unsupported content.\n"
        "- Explanations must reflect reasoning traceable to the source (without fabricating "
        "citations).\n\n"
        "Difficulty: assign Easy, Medium, or Hard based on academic depth.\n"
        "Cross-document synthesis: ensure at least 1–2 questions synthesize or compare "
        "information across multiple provided documents when multiple documents are present.\n\n"
        'Return ONLY a valid JSON object with a single key "questions" whose value is an '
        "array of question objects. Each question object must include:\n"
        "question (string), options (array of exactly 4 strings), correct_index (integer 0–3),\n"
        "explanation (string), topics (array of strings from the allowed list only),\n"
        "difficulty (Easy|Medium|Hard), answer (string) — must equal options[correct_index].\n"
        f"{weak_block}"
    )


def _build_question_response_schema(allowed_topic_names, requested_question_count):
    question_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "question",
            "options",
            "correct_index",
            "explanation",
            "topics",
            "difficulty",
            "answer",
        ],
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 4,
            },
            "correct_index": {"type": "integer", "minimum": 0, "maximum": 3},
            "explanation": {"type": "string"},
            "topics": {
                "type": "array",
                "items": {"type": "string", "enum": allowed_topic_names},
                "minItems": 1,
            },
            "difficulty": {
                "type": "string",
                "enum": ["Easy", "Medium", "Hard"],
            },
            "answer": {"type": "string"},
        },
    }
    return {
        "name": "quiz_questions",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["questions"],
            "properties": {
                "questions": {
                    "type": "array",
                    "items": question_item_schema,
                    "minItems": requested_question_count,
                    "maxItems": requested_question_count,
                },
            },
        },
    }


def _response(status_code, payload, allow_methods="POST,OPTIONS"):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": allow_methods,
            "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _now_epoch():
    return int(time.time())


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(str(raw).strip())


def _generation_slot_id(course_id, document_ids):
    """Stable id of the row that names the active generation for a selection."""
    key = "generation-slot:%s:%s" % (course_id, "|".join(sorted(document_ids)))
    return str(uuid5(_GENERATION_NAMESPACE, key))


def _question_id(set_id, index):
    return str(uuid5(_GENERATION_NAMESPACE, "%s:%s" % (set_id, index)))


def _seed_for_generation(generation_id):
    digest = hashlib.sha256(generation_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") or 1


def _is_conditional_check_failed(exc):
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _failure_code_for_exception(exc):
    if isinstance(exc, GenerationError):
        return exc.code
    if isinstance(exc, ContentSelectionError):
        return _CONTENT_SELECTION_CODE_MAP.get(exc.code, "CONTENT_SELECTION_FAILED")
    if isinstance(exc, ValueError):
        return "LLM_INVALID_RESPONSE"
    return "INTERNAL_ERROR"


def _normalize_question(item, *, canonical_lookup=None):
    if not isinstance(item, dict):
        return None

    question = item.get("question")
    options = item.get("options")
    correct_index = item.get("correct_index")
    explanation = item.get("explanation")
    topics = item.get("topics")
    topic = item.get("topic")
    answer = item.get("answer")
    difficulty = item.get("difficulty")

    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(explanation, str) or not explanation.strip():
        return None
    if not isinstance(options, list) or len(options) != 4:
        return None
    if any(not isinstance(opt, str) or not opt.strip() for opt in options):
        return None
    normalized_options = [opt.strip() for opt in options]

    resolved_correct_index = correct_index if isinstance(correct_index, int) else None
    if resolved_correct_index is None or resolved_correct_index < 0 or resolved_correct_index > 3:
        resolved_correct_index = None
        if isinstance(answer, str) and answer.strip():
            normalized_answer = answer.strip()
            for idx, option in enumerate(normalized_options):
                if option == normalized_answer:
                    resolved_correct_index = idx
                    break
            if resolved_correct_index is None:
                lowered_answer = normalized_answer.lower()
                for idx, option in enumerate(normalized_options):
                    if option.lower() == lowered_answer:
                        resolved_correct_index = idx
                        break
    if resolved_correct_index is None:
        return None

    if isinstance(topics, list):
        raw_topic_strings = [
            str(t) for t in topics if isinstance(t, str) and t.strip()
        ]
    elif isinstance(topic, str) and topic.strip():
        raw_topic_strings = [topic.strip()]
    else:
        raw_topic_strings = []

    canonical_topics = []
    seen_topics = set()
    lookup = canonical_lookup or {}
    for raw in raw_topic_strings:
        key = raw.strip().casefold()
        canonical = lookup.get(key)
        if canonical and canonical not in seen_topics:
            seen_topics.add(canonical)
            canonical_topics.append(canonical)
    if not canonical_topics:
        canonical_topics = [FALLBACK_TOPICS[0]["en"]]

    if isinstance(difficulty, str) and difficulty.strip():
        normalized_difficulty = difficulty.strip().title()
    else:
        normalized_difficulty = "Medium"
    if normalized_difficulty not in _ALLOWED_DIFFICULTIES:
        normalized_difficulty = "Medium"

    return {
        "question": question.strip(),
        "options": normalized_options,
        "correct_index": resolved_correct_index,
        "explanation": explanation.strip(),
        "topics": canonical_topics,
        "difficulty": normalized_difficulty,
    }


def _parse_valid_questions(raw_response, canonical_lookup=None):
    cleaned = extract_json_payload(raw_response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response is not valid JSON after cleaning: {exc}") from exc
    if isinstance(parsed, dict):
        questions = parsed.get("questions")
        if not isinstance(questions, list):
            raise ValueError("Model response object must include a 'questions' JSON array")
        parsed = questions
    elif not isinstance(parsed, list):
        raise ValueError(
            "Model response must be a JSON object with a 'questions' array"
        )

    valid = []
    discarded = 0
    for item in parsed:
        normalized = _normalize_question(item, canonical_lookup=canonical_lookup)
        if normalized is None:
            discarded += 1
            continue
        valid.append(normalized)
    return valid, discarded, cleaned


def _get_claims(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )


def _parse_api_request(event):
    claims = _get_claims(event)
    if not claims.get("sub"):
        return None, _response(401, {"message": "Unauthorized: missing user identity"})

    path_parameters = event.get("pathParameters", {})
    course_id = path_parameters.get("courseId")
    if not course_id:
        return None, _response(400, {"message": "Missing path parameter: courseId"})

    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded", False):
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    body = json.loads(raw_body)

    document_ids = body.get("documentIds")
    if not isinstance(document_ids, list) or not document_ids:
        return None, _response(400, {"message": "Field 'documentIds' must be a non-empty list"})
    if any(not isinstance(doc_id, str) or not doc_id.strip() for doc_id in document_ids):
        return None, _response(400, {"message": "Field 'documentIds' must contain non-empty strings"})

    normalized_document_ids = []
    seen_document_ids = set()
    for doc_id in document_ids:
        stripped = doc_id.strip()
        if stripped in seen_document_ids:
            continue
        seen_document_ids.add(stripped)
        normalized_document_ids.append(stripped)

    has_requested_count = "requested_question_count" in body
    has_quiz_language = "quiz_language" in body
    raw_requested_count = body.get("requested_question_count")
    raw_quiz_language = body.get("quiz_language")

    if has_requested_count or has_quiz_language:
        if not has_requested_count or not has_quiz_language:
            return None, _response(
                400,
                {
                    "message": (
                        "Fields 'requested_question_count' and 'quiz_language' must "
                        "both be provided when either is present"
                    )
                },
            )
        if not isinstance(raw_requested_count, int) or isinstance(raw_requested_count, bool):
            return None, _response(
                400,
                {"message": "Field 'requested_question_count' must be an integer"},
            )
        if raw_requested_count not in _ALLOWED_REQUESTED_QUESTION_COUNTS:
            return None, _response(
                400,
                {
                    "message": (
                        "Field 'requested_question_count' must be one of: "
                        "5, 10, 15, 20"
                    )
                },
            )
        if not isinstance(raw_quiz_language, str) or not raw_quiz_language.strip():
            return None, _response(
                400,
                {"message": "Field 'quiz_language' must be a non-empty string"},
            )
        quiz_language = raw_quiz_language.strip().lower()
        if quiz_language not in _ALLOWED_QUIZ_LANGUAGES:
            return None, _response(
                400,
                {"message": "Field 'quiz_language' must be 'he' or 'en'"},
            )
        requested_question_count = raw_requested_count
    else:
        requested_question_count = 5
        quiz_language = "he"

    raw_focus_weak = body.get("focus_weak_topics")
    if raw_focus_weak is not None and not isinstance(raw_focus_weak, bool):
        return None, _response(
            400,
            {"message": "Field 'focus_weak_topics' must be a boolean"},
        )
    focus_weak_topics = raw_focus_weak is True

    return {
        "course_id": course_id,
        "document_ids": normalized_document_ids,
        "requested_by": claims["sub"],
        "requested_question_count": requested_question_count,
        "quiz_language": quiz_language,
        "focus_weak_topics": focus_weak_topics,
    }, None


def _validate_documents(course_id, document_ids, correlation_id):
    """Accept only documents whose own processing pipeline produced usable text.

    Quiz-generation history is irrelevant here: a document that failed a
    previous generation attempt is still a valid source.
    """
    for document_id in document_ids:
        result = _documents_table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            return _response(
                404,
                {
                    "message": "Document not found: %s" % document_id,
                    "code": "DOCUMENT_NOT_FOUND",
                },
            )
        if item.get("course_id") != course_id:
            return _response(403, {"message": "Forbidden for document: %s" % document_id})
        if not is_usable_quiz_source(item):
            return _response(
                400,
                {
                    "message": "Document is not processed yet: %s" % document_id,
                    "code": "DOCUMENT_NOT_PROCESSED",
                },
            )
    logger.info("cid=%s validated_documents=%s", correlation_id, len(document_ids))
    return None


def _lease_is_live(row, now):
    status = str(row.get("generation_status") or "")
    if status not in ("PENDING", "GENERATING"):
        return False
    lease = row.get("lease_expires_at")
    try:
        return lease is not None and int(lease) > int(now)
    except (TypeError, ValueError):
        return False


def _claim_generation_slot(course_id, document_ids, generation_id, correlation_id):
    """Take the generation slot for this course and selection.

    The slot row only names the generation that holds it; whether that
    generation is still alive is decided by its own record's lease, so
    generation state stays the single source of truth. Documents are not
    involved in this lock.
    """
    slot_id = _generation_slot_id(course_id, document_ids)
    slot = _question_sets_table.get_item(
        Key={"set_id": slot_id},
        ConsistentRead=True,
    ).get("Item") or {}
    prior_generation_id = slot.get("active_generation_id")

    if prior_generation_id:
        row = _question_sets_table.get_item(
            Key={"set_id": _generation_set_id(prior_generation_id)},
            ConsistentRead=True,
        ).get("Item") or {}
        if _lease_is_live(row, _now_epoch()):
            return _response(409, {"message": "Quiz generation already in progress"})
        revoke_error = _revoke_generations([prior_generation_id], correlation_id)
        if revoke_error:
            return revoke_error
        condition = "active_generation_id = :prior"
    else:
        condition = "attribute_not_exists(active_generation_id)"

    values = {
        ":gid": generation_id,
        ":course": course_id,
        ":docs": list(document_ids),
    }
    if prior_generation_id:
        values[":prior"] = prior_generation_id
    try:
        _question_sets_table.update_item(
            Key={"set_id": slot_id},
            UpdateExpression=(
                "SET active_generation_id = :gid, generation_course_id = :course, "
                "document_ids = :docs"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_check_failed(exc):
            logger.info(
                "cid=%s generation_slot_contended course_id=%s",
                correlation_id,
                course_id,
            )
            return _response(409, {"message": "Quiz generation already in progress"})
        logger.warning(
            "cid=%s generation_slot_unavailable course_id=%s error_type=%s",
            correlation_id,
            course_id,
            type(exc).__name__,
        )
        return _response(
            500,
            {
                "message": "Quiz generation could not be locked",
                "code": "GENERATION_LOCK_UNAVAILABLE",
            },
        )
    return None


def _revoke_generations(stale_generation_ids, correlation_id):
    for generation_id in stale_generation_ids:
        try:
            _question_sets_table.update_item(
                Key={"set_id": _generation_set_id(generation_id)},
                UpdateExpression="SET generation_status = :revoked",
                ConditionExpression=(
                    "generation_status IN (:pending, :generating) AND "
                    "(attribute_not_exists(lease_expires_at) OR lease_expires_at <= :now)"
                ),
                ExpressionAttributeValues={
                    ":revoked": "REVOKED",
                    ":pending": "PENDING",
                    ":generating": "GENERATING",
                    ":now": _now_epoch(),
                },
            )
            logger.info(
                "cid=%s generation_revoked generation_id=%s",
                correlation_id,
                generation_id,
            )
        except ClientError as exc:
            if _is_conditional_check_failed(exc):
                row = _question_sets_table.get_item(
                    Key={"set_id": _generation_set_id(generation_id)},
                    ConsistentRead=True,
                ).get("Item") or {}
                if row.get("generation_status") in ("PENDING", "GENERATING"):
                    return _response(
                        409, {"message": "Quiz generation already in progress"}
                    )
                continue
            logger.warning(
                "cid=%s generation_lock_unavailable generation_id=%s error_type=%s",
                correlation_id,
                generation_id,
                type(exc).__name__,
            )
            return _response(
                500,
                {
                    "message": "Quiz generation could not be locked",
                    "code": "GENERATION_LOCK_UNAVAILABLE",
                },
            )
    return None


def _create_generation_record(generation_id, course_id, document_ids, correlation_id):
    item = {
        "set_id": _generation_set_id(generation_id),
        "generation_id": generation_id,
        "generation_status": "PENDING",
        "lease_expires_at": _now_epoch() + _DISPATCH_LEASE_SECONDS,
        "generation_course_id": course_id,
        "document_ids": list(document_ids),
    }
    _question_sets_table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(set_id)",
    )
    logger.info(
        "cid=%s generation_record_created generation_id=%s set_id=%s",
        correlation_id,
        generation_id,
        item["set_id"],
    )
    return item


def _abort_generation(generation_id, correlation_id, code="DISPATCH_FAILED"):
    """Release a generation that never reached a worker."""
    try:
        _question_sets_table.update_item(
            Key={"set_id": _generation_set_id(generation_id)},
            UpdateExpression="SET generation_status = :aborted, failure_code = :code",
            ConditionExpression="generation_status IN (:pending, :generating)",
            ExpressionAttributeValues={
                ":aborted": "ABORTED",
                ":code": code,
                ":pending": "PENDING",
                ":generating": "GENERATING",
            },
        )
    except ClientError as exc:
        if not _is_conditional_check_failed(exc):
            logger.warning(
                "cid=%s generation_aborted generation_id=%s failure_code=%s error_type=%s",
                correlation_id,
                generation_id,
                code,
                type(exc).__name__,
            )
            return
    logger.info(
        "cid=%s generation_aborted generation_id=%s failure_code=%s",
        correlation_id,
        generation_id,
        code,
    )


def _difficulty_breakdown(questions):
    breakdown = {"easy": 0, "medium": 0, "hard": 0}
    for question in questions:
        level = str(question.get("difficulty") or "").strip().lower()
        if level in breakdown:
            breakdown[level] += 1
    return breakdown


def _default_set_name(created_at):
    date_label = created_at[:10]
    return f"Quiz from {date_label}"


def _extract_progress_matrix(item):
    if not item:
        return {}
    matrix = item.get("matrix")
    if matrix is None or not isinstance(matrix, dict):
        return {}
    return matrix


def _empty_weak_focus_result():
    return {
        "prioritized_weak_topics": [],
        "applied_focus_weak_topics": False,
        "progress_found": False,
        "weak_count_before_intersection": 0,
        "weak_count_after_intersection": 0,
    }


def _resolve_weak_topic_focus(user_name, course_id, canonical_lookup, correlation_id):
    result = _empty_weak_focus_result()
    if not user_name or not USER_PROGRESS_TABLE:
        return result
    try:
        progress_table = _dynamodb.Table(USER_PROGRESS_TABLE)
        item_result = progress_table.get_item(
            Key={"user_name": user_name, "course_id": course_id}
        )
        matrix = _extract_progress_matrix(item_result.get("Item"))
        if not matrix:
            return result
        result["progress_found"] = True
        scored = compute_topic_scores(matrix)
        result["weak_count_before_intersection"] = sum(
            1 for topic in scored if topic.get("status") == "weak"
        )
        prioritized = select_prioritized_weak_topics(
            matrix, canonical_lookup=canonical_lookup, limit=5
        )
        result["prioritized_weak_topics"] = prioritized
        result["weak_count_after_intersection"] = len(prioritized)
        result["applied_focus_weak_topics"] = len(prioritized) > 0
        return result
    except Exception:
        logger.warning(
            "cid=%s weak_focus_resolve_failed course_id=%s",
            correlation_id,
            course_id,
        )
        return _empty_weak_focus_result()


def _question_set_generation_metadata(applied_focus_weak_topics, prioritized_weak_topics):
    metadata = {
        "generation_mode": (
            "WEAKNESS_FOCUSED" if applied_focus_weak_topics else "NORMAL"
        ),
    }
    if applied_focus_weak_topics:
        metadata["focused_topics"] = list(prioritized_weak_topics)
    return metadata


def _selection_config(model_name, seed):
    limits = model_limits(model_name)
    config = SelectionConfig(
        model_name=model_name,
        context_window_tokens=_env_int(
            "QUIZ_MODEL_CONTEXT_WINDOW_TOKENS", limits.context_window_tokens
        ),
        max_output_tokens=_env_int(
            "QUIZ_MODEL_MAX_OUTPUT_TOKENS", limits.max_output_tokens
        ),
        operational_input_token_budget=_env_int("QUIZ_INPUT_TOKEN_BUDGET", 120000),
        seed=seed,
        min_passage_chars=_env_int("QUIZ_MIN_PASSAGE_CHARS", MIN_PASSAGE_CHARS),
        target_passage_chars=_env_int("QUIZ_TARGET_PASSAGE_CHARS", TARGET_PASSAGE_CHARS),
        max_passages_per_document=_env_int(
            "QUIZ_MAX_PASSAGES_PER_DOCUMENT", MAX_PASSAGES_PER_DOCUMENT
        ),
    )
    return config, _env_int("QUIZ_SAFETY_MARGIN_TOKENS", 1500)


def _s3_reader(source_keys):
    def read_text(document_id):
        key = source_keys.get(document_id)
        if not key:
            raise GenerationError("DOCUMENT_NOT_PROCESSED")
        try:
            s3_obj = _s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
            return s3_obj["Body"].read().decode("utf-8", errors="replace")
        except ClientError as exc:
            logger.warning(
                "document_read_failed document_id=%s error_type=%s",
                document_id,
                type(exc).__name__,
            )
            raise GenerationError("DOCUMENT_READ_FAILED") from exc

    return read_text


def _begin_generation(generation_id, worker_request_id, correlation_id):
    if not worker_request_id:
        return {"ok": False, "code": "OWNERSHIP_LOST"}
    now = _now_epoch()
    try:
        _question_sets_table.update_item(
            Key={"set_id": _generation_set_id(generation_id)},
            UpdateExpression=(
                "SET generation_status = :generating, lease_expires_at = :t, "
                "worker_request_id = :w, worker_started_at = :started"
            ),
            ConditionExpression=(
                "generation_id = :gid AND "
                "(generation_status = :pending OR "
                "(generation_status = :generating AND lease_expires_at <= :now))"
            ),
            ExpressionAttributeValues={
                ":generating": "GENERATING",
                ":t": now + _RUNNING_LEASE_SECONDS,
                ":w": worker_request_id,
                ":started": datetime.now(timezone.utc).isoformat(),
                ":gid": generation_id,
                ":pending": "PENDING",
                ":now": now,
            },
        )
        return {"ok": True}
    except ClientError as exc:
        if not _is_conditional_check_failed(exc):
            raise
    row = _question_sets_table.get_item(
        Key={"set_id": _generation_set_id(generation_id)},
        ConsistentRead=True,
    ).get("Item") or {}
    status = row.get("generation_status")
    if row.get("generation_id") == generation_id and _lease_is_live(row, _now_epoch()):
        logger.info(
            "cid=%s duplicate_worker_in_progress generation_id=%s",
            correlation_id,
            generation_id,
        )
        return {"ok": True, "in_progress": True}
    if status == "READY":
        return {"ok": True, "duplicate": True}
    if status in ("REVOKED", "ABORTED", "FAILED"):
        if status != "FAILED":
            logger.info(
                "cid=%s ownership_lost generation_id=%s status=%s",
                correlation_id,
                generation_id,
                status,
            )
        return {"ok": False, "code": "OWNERSHIP_LOST" if status != "FAILED" else "FAILED"}
    logger.info(
        "cid=%s ownership_lost generation_id=%s status=%s",
        correlation_id,
        generation_id,
        status,
    )
    return {"ok": False, "code": "OWNERSHIP_LOST"}


def _fail_generation(
    code,
    generation_id,
    correlation_id,
    from_status="GENERATING",
    worker_request_id=None,
):
    """Record the failure on the generation. Source documents are untouched."""
    # A worker may fail only the generation it still owns. PENDING failures
    # occur before a worker has claimed the row, so they need no worker ID.
    if from_status == "GENERATING" and not worker_request_id:
        return False
    condition = "generation_id = :gid AND generation_status = :from_status"
    values = {
        ":failed": "FAILED",
        ":code": code,
        ":from_status": from_status,
        ":gid": generation_id,
    }
    if from_status == "GENERATING":
        condition += " AND worker_request_id = :worker"
        values[":worker"] = worker_request_id
    try:
        _question_sets_table.update_item(
            Key={"set_id": _generation_set_id(generation_id)},
            UpdateExpression="SET generation_status = :failed, failure_code = :code",
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_check_failed(exc):
            return False
        raise
    logger.info(
        "cid=%s generation_outcome status=FAILED failure_code=%s set_id=%s",
        correlation_id,
        code,
        _generation_set_id(generation_id),
    )
    return True


def _question_put_actions(set_id, questions):
    serializer = TypeSerializer()
    actions = []
    for index, question in enumerate(questions):
        row = {
            "question_id": _question_id(set_id, index),
            "set_id": set_id,
            "question": question["question"],
            "options": list(question["options"]),
            "correct_index": int(question["correct_index"]),
            "explanation": question["explanation"],
            "topics": list(question["topics"]),
            "difficulty": question["difficulty"],
        }
        actions.append(
            {
                "Put": {
                    "TableName": QUESTIONS_TABLE,
                    "Item": serializer.serialize(row)["M"],
                }
            }
        )
    return actions


def _ready_set_update(set_id, fields, worker_request_id):
    serializer = TypeSerializer()
    names = {}
    values = {
        ":generating": serializer.serialize("GENERATING"),
        ":worker": serializer.serialize(worker_request_id),
    }
    assignments = []
    for index, key in enumerate(fields):
        name_token = "#f%d" % index
        value_token = ":f%d" % index
        names[name_token] = key
        values[value_token] = serializer.serialize(fields[key])
        assignments.append("%s = %s" % (name_token, value_token))
    return {
        "Update": {
            "TableName": QUESTION_SETS_TABLE,
            "Key": serializer.serialize({"set_id": set_id})["M"],
            "UpdateExpression": "SET " + ", ".join(assignments),
            "ConditionExpression": (
                "generation_status = :generating AND worker_request_id = :worker"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _build_transact_items(set_id, questions, ready_fields, worker_request_id):
    actions = _question_put_actions(set_id, questions)
    actions.append(_ready_set_update(set_id, ready_fields, worker_request_id))
    if len(actions) > _MAX_TRANSACT_ACTIONS:
        raise GenerationError("PERSISTENCE_FAILED")
    return actions


def _is_lost_commit_race(exc):
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or []
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


def _commit_generation(
    generation_id,
    set_id,
    questions,
    ready_fields,
    correlation_id,
    worker_request_id,
):
    if not worker_request_id:
        raise GenerationError("OWNERSHIP_LOST")
    actions = _build_transact_items(set_id, questions, ready_fields, worker_request_id)
    token = (worker_request_id or str(uuid4()))[:36]

    try:
        _dynamodb_transactions.transact_write_items(
            TransactItems=actions,
            ClientRequestToken=token,
        )

    except ClientError as exc:
        if _is_lost_commit_race(exc):
            row = _question_sets_table.get_item(
                Key={"set_id": set_id},
                ConsistentRead=True,
            ).get("Item") or {}

            if row.get("generation_status") == "READY":
                logger.info(
                    "cid=%s commit_lost_race generation_id=%s set_id=%s",
                    correlation_id,
                    generation_id,
                    set_id,
                )
                return "lost_race"

            logger.info(
                "cid=%s ownership_lost generation_id=%s set_id=%s status=%s",
                correlation_id,
                generation_id,
                set_id,
                row.get("generation_status"),
            )

            raise GenerationError("OWNERSHIP_LOST") from exc

        response = exc.response or {}
        error = response.get("Error", {})
        reasons = response.get("CancellationReasons") or []

        logger.error(
            "cid=%s persistence_failed generation_id=%s "
            "error_type=%s error_code=%s error_message=%s "
            "cancellation_reasons=%s",
            correlation_id,
            generation_id,
            type(exc).__name__,
            error.get("Code"),
            error.get("Message"),
            reasons,
        )

        raise GenerationError("PERSISTENCE_FAILED") from exc

    return "committed"


def _generate_questions_worker(
    course_id,
    document_ids,
    correlation_id,
    requested_question_count,
    quiz_language,
    requested_by=None,
    requested_focus_weak_topics=False,
    generation_id=None,
    worker_request_id=None,
    commit_state=None,
):
    if commit_state is None:
        commit_state = {}
    topic_lists = []
    source_keys = {}
    source_document_names = []
    refs = []
    for document_id in document_ids:
        result = _documents_table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            raise GenerationError("DOCUMENT_NOT_FOUND")
        if not is_usable_quiz_source(item):
            raise GenerationError("DOCUMENT_NOT_PROCESSED")
        processed_key = item["s3_processed_key"]

        item = ensure_document_topics(dict(item))
        topic_lists.append(item["topics"])
        label = (
            item.get("original_file_name")
            or item.get("originalFileName")
            or "Document %s" % (len(source_document_names) + 1)
        )
        source_document_names.append(label)
        source_keys[document_id] = processed_key
        refs.append(DocumentRef(document_id, label=label))

    unified_topics = dedupe_topics_by_en(topic_lists)
    allowed_en = allowed_en_topic_names(unified_topics)
    canonical_lookup = build_canonical_topic_lookup(allowed_en)
    logger.info(
        "cid=%s allowed_topics=%s count=%s",
        correlation_id,
        allowed_en,
        len(allowed_en),
    )

    prioritized_weak_topics = []
    applied_focus_weak_topics = False
    weak_focus_result = _empty_weak_focus_result()
    if requested_focus_weak_topics and requested_by:
        weak_focus_result = _resolve_weak_topic_focus(
            requested_by, course_id, canonical_lookup, correlation_id
        )
        prioritized_weak_topics = weak_focus_result["prioritized_weak_topics"]
        applied_focus_weak_topics = weak_focus_result["applied_focus_weak_topics"]
    logger.info(
        "cid=%s weak_focus_requested=%s progress_found=%s "
        "weak_before_intersection=%s weak_after_intersection=%s applied=%s",
        correlation_id,
        requested_focus_weak_topics,
        weak_focus_result["progress_found"],
        weak_focus_result["weak_count_before_intersection"],
        weak_focus_result["weak_count_after_intersection"],
        applied_focus_weak_topics,
    )
    if applied_focus_weak_topics:
        logger.info(
            "cid=%s prioritized_weak_topics=%s",
            correlation_id,
            prioritized_weak_topics,
        )

    _, model_name = openai_config()
    schema = _build_question_response_schema(allowed_en, requested_question_count)
    system_prompt = _build_system_prompt(
        allowed_en,
        requested_question_count,
        quiz_language,
        prioritized_weak_topics=(
            prioritized_weak_topics if applied_focus_weak_topics else None
        ),
    )
    counter = get_token_counter(model_name)
    config, safety_margin = _selection_config(
        model_name, _seed_for_generation(generation_id)
    )
    overhead = count_overhead_tokens(
        counter,
        system_prompt=system_prompt,
        schema_json=json.dumps(schema, ensure_ascii=False),
        extra_hints=_SHORT_SOURCE_HINT,
        reserved_output=reserved_output_tokens(
            requested_question_count, max_output_tokens=config.max_output_tokens
        ),
        safety_margin=safety_margin,
    )
    selection = select_quiz_content_streaming(
        refs,
        read_text=_s3_reader(source_keys),
        config=config,
        overhead=overhead,
        token_counter=counter,
    )
    metadata = selection.metadata
    logger.info(
        "cid=%s content_selection_complete generation_id=%s mode=%s "
        "unique_document_count=%s context_tokens_measured=%s "
        "overhead_tokens_declared=%s total_input_tokens=%s "
        "source_token_allowance=%s budget_iterations=%s global_scale_permille=%s",
        correlation_id,
        generation_id,
        metadata.mode,
        metadata.unique_document_count,
        metadata.context_tokens_measured,
        metadata.overhead_tokens_declared,
        metadata.context_tokens_measured + metadata.overhead_tokens_declared,
        metadata.source_token_allowance,
        metadata.budget_iterations,
        metadata.global_scale_permille,
    )
    user_content = selection.context
    if metadata.selected_source_chars_total < _SHORT_SOURCE_HINT_THRESHOLD:
        user_content = "%s\n\n%s" % (selection.context, _SHORT_SOURCE_HINT)

    client = get_openai_client()
    started = time.monotonic()
    try:
        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            timeout=_OPENAI_TIMEOUT_SECONDS,
            response_format={"type": "json_schema", "json_schema": schema},
        )
    except GenerationError:
        raise
    except Exception as exc:
        logger.error(
            "cid=%s llm_request_failed error_type=%s openai_latency_ms=%s",
            correlation_id,
            type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )
        raise GenerationError("LLM_REQUEST_FAILED") from exc
    logger.info(
        "cid=%s openai_latency_ms=%s openai_response_chars=%s",
        correlation_id,
        int((time.monotonic() - started) * 1000),
        len(completion.choices[0].message.content or ""),
    )
    raw_response = completion.choices[0].message.content or ""
    try:
        valid_questions, discarded_count, _cleaned = _parse_valid_questions(
            raw_response, canonical_lookup=canonical_lookup
        )
    except ValueError as exc:
        raise GenerationError("LLM_INVALID_RESPONSE") from exc
    if not valid_questions or len(valid_questions) != requested_question_count:
        logger.error(
            "cid=%s llm_invalid_response validated=%s discarded=%s requested=%s",
            correlation_id,
            len(valid_questions),
            discarded_count,
            requested_question_count,
        )
        raise GenerationError("LLM_INVALID_RESPONSE")

    set_id = _generation_set_id(generation_id)
    created_at = datetime.now(timezone.utc).isoformat()
    default_set_name = _default_set_name(created_at)
    ready_fields = {
        "generation_status": "READY",
        "course_id": course_id,
        "created_at": created_at,
        "name": default_set_name,
        "set_name": default_set_name,
        "question_count": len(valid_questions),
        "difficulty_breakdown": _difficulty_breakdown(valid_questions),
        "document_ids": list(document_ids),
        "source_document_names": list(source_document_names),
        "title": "Combined Quiz - %s Materials" % len(document_ids),
        "quiz_language": quiz_language,
        "requested_question_count": requested_question_count,
        "generation_id": generation_id,
    }
    ready_fields.update(
        _question_set_generation_metadata(
            applied_focus_weak_topics, prioritized_weak_topics
        )
    )
    outcome = _commit_generation(
        generation_id,
        set_id,
        valid_questions,
        ready_fields,
        correlation_id,
        worker_request_id,
    )
    commit_state["committed"] = True
    logger.info(
        "cid=%s generation_outcome status=%s failure_code=%s set_id=%s requested=%s validated=%s",
        correlation_id,
        "READY",
        "",
        set_id,
        requested_question_count,
        len(valid_questions),
    )
    return {
        "ok": True,
        "committed": True,
        "set_id": set_id,
        "duplicate": outcome == "lost_race",
    }



def _invoke_worker_async(payload, correlation_id):
    function_name = os.environ.get("WORKER_FUNCTION_NAME")
    if not function_name:
        raise RuntimeError("WORKER_FUNCTION_NAME is not configured")
    _lambda.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    logger.info("cid=%s worker_enqueued function_name=%s", correlation_id, function_name)


def worker_handler(event, context):
    correlation_id = event.get("apiRequestId") or context.aws_request_id
    generation_id = event.get("generationId")
    worker_request_id = context.aws_request_id
    course_id = event.get("courseId")
    document_ids = event.get("documentIds") or []
    requested_by = event.get("requestedBy")
    requested_question_count = event.get("requestedQuestionCount", 5)
    quiz_language = (event.get("quizLanguage") or "he").strip().lower()
    requested_focus_weak_topics = bool(event.get("focusWeakTopics"))
    if requested_question_count not in _ALLOWED_REQUESTED_QUESTION_COUNTS:
        requested_question_count = 5
    if quiz_language not in _ALLOWED_QUIZ_LANGUAGES:
        quiz_language = "he"
    logger.info(
        "cid=%s worker_start course_id=%s doc_count=%s generation_id=%s",
        correlation_id,
        course_id,
        len(document_ids),
        generation_id,
    )
    if not generation_id:
        logger.error("cid=%s worker_missing_generation_id course_id=%s", correlation_id, course_id)
        return {"ok": False, "code": "INTERNAL_ERROR"}
    if requested_by:
        course_row = _courses_table.get_item(Key={"course_id": course_id}).get("Item") or {}
        if course_row.get("owner_id") != requested_by:
            logger.warning(
                "cid=%s worker_rejected owner_mismatch course_id=%s",
                correlation_id,
                course_id,
            )
            _fail_generation(
                "UNAUTHORIZED",
                generation_id,
                correlation_id,
                from_status="PENDING",
            )
            return {"ok": False, "code": "UNAUTHORIZED"}
    begin = _begin_generation(generation_id, worker_request_id, correlation_id)
    if begin.get("in_progress"):
        return {"ok": True, "duplicate": True, "committed": False}
    if begin.get("duplicate"):
        return {"ok": True, "duplicate": True, "committed": True}
    if not begin.get("ok"):
        return {"ok": False, "code": begin.get("code")}
    commit_state = {"committed": False}
    try:
        return _generate_questions_worker(
            course_id,
            document_ids,
            correlation_id,
            requested_question_count,
            quiz_language,
            requested_by=requested_by,
            requested_focus_weak_topics=requested_focus_weak_topics,
            generation_id=generation_id,
            worker_request_id=worker_request_id,
            commit_state=commit_state,
        )
    except Exception as exc:
        code = _failure_code_for_exception(exc)
        if code == "OWNERSHIP_LOST":
            logger.info(
                "cid=%s ownership_lost generation_id=%s",
                correlation_id,
                generation_id,
            )
            return {"ok": False, "code": "OWNERSHIP_LOST"}
        if commit_state.get("committed"):
            logger.error(
                "cid=%s post_commit_error_ignored generation_id=%s set_id=%s error_type=%s",
                correlation_id,
                generation_id,
                _generation_set_id(generation_id),
                type(exc).__name__,
            )
            return {"ok": True, "committed": True}
        logger.error(
            "cid=%s worker_failed course_id=%s failure_code=%s error_type=%s",
            correlation_id,
            course_id,
            code,
            type(exc).__name__,
        )
        _fail_generation(
            code,
            generation_id,
            correlation_id,
            worker_request_id=worker_request_id,
        )
        return {"ok": False, "code": code}


def api_handler(event, context):
    correlation_id = context.aws_request_id
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"})

        parsed, error_response = _parse_api_request(event)
        if error_response:
            return error_response

        course_id = parsed["course_id"]
        document_ids = parsed["document_ids"]
        requested_by = parsed["requested_by"]
        logger.info(
            "cid=%s api_request_received course_id=%s doc_count=%s",
            correlation_id,
            course_id,
            len(document_ids),
        )

        gate = require_course_owner(_courses_table, course_id, requested_by)
        if gate:
            status, body = gate
            return _response(status, body)

        error_response = _validate_documents(course_id, document_ids, correlation_id)
        if error_response:
            return error_response

        generation_id = str(uuid4())
        # The record exists before the slot is claimed, so a request that is
        # still starting up already holds a live lease and cannot be stolen
        # from by a concurrent request.
        _create_generation_record(
            generation_id, course_id, document_ids, correlation_id
        )
        slot_error = _claim_generation_slot(
            course_id, document_ids, generation_id, correlation_id
        )
        if slot_error:
            _abort_generation(
                generation_id, correlation_id, code="GENERATION_SUPERSEDED"
            )
            return slot_error

        worker_payload = {
            "courseId": course_id,
            "documentIds": document_ids,
            "requestedBy": requested_by,
            "apiRequestId": correlation_id,
            "generationId": generation_id,
            "requestedQuestionCount": parsed["requested_question_count"],
            "quizLanguage": parsed["quiz_language"],
            "focusWeakTopics": parsed["focus_weak_topics"],
        }
        try:
            logger.info("cid=%s invoking_worker generation_id=%s", correlation_id, generation_id)
            _invoke_worker_async(worker_payload, correlation_id)
        except Exception as exc:
            logger.error(
                "cid=%s dispatch_failed generation_id=%s error_type=%s",
                correlation_id,
                generation_id,
                type(exc).__name__,
            )
            _abort_generation(generation_id, correlation_id)
            return _response(
                500,
                {
                    "message": "Failed to start async generation job",
                    "code": "DISPATCH_FAILED",
                },
            )

        return _response(
            202,
            {
                "message": "Question generation started",
                "course_id": course_id,
                "documents_queued": len(document_ids),
                "request_id": correlation_id,
                "generation_id": generation_id,
            },
        )
    except Exception as exc:
        logger.error(
            "cid=%s unhandled_error error_type=%s",
            correlation_id,
            type(exc).__name__,
        )
        return _response(500, {"message": "Internal server error"})
