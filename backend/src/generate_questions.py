import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from course_access import require_course_owner
from topic_scoring import compute_topic_scores, select_prioritized_weak_topics
from openai_helpers import (
    FALLBACK_TOPICS,
    QUIZ_MAX_SOURCE_CHARS,
    allowed_en_topic_names,
    build_canonical_topic_lookup,
    dedupe_topics_by_en,
    ensure_document_topics,
    extract_json_payload,
    get_openai_client,
    openai_config,
    truncate_source_text,
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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
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


def _truncate_for_log(value, limit=3000):
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _allocate_budgets(texts, total_budget):
    if not texts or total_budget <= 0:
        return [0 for _ in texts]

    budgets = [0] * len(texts)
    remaining_budget = total_budget
    remaining_indexes = list(range(len(texts)))

    while remaining_indexes and remaining_budget > 0:
        share = max(1, remaining_budget // len(remaining_indexes))
        next_indexes = []
        for idx in remaining_indexes:
            remaining_len = max(0, len(texts[idx]) - budgets[idx])
            take = min(share, remaining_len, remaining_budget)
            budgets[idx] += take
            remaining_budget -= take
            if budgets[idx] < len(texts[idx]) and remaining_budget > 0:
                next_indexes.append(idx)
            if remaining_budget <= 0:
                break
        remaining_indexes = next_indexes

    return budgets


def _build_balanced_context(texts):
    budgets = _allocate_budgets(texts, QUIZ_MAX_SOURCE_CHARS)
    parts = [texts[idx][:budgets[idx]] for idx in range(len(texts)) if budgets[idx] > 0]
    return "\n\n".join(parts), budgets


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

    normalized_document_ids = [doc_id.strip() for doc_id in document_ids]

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
    source_keys = {}
    for document_id in document_ids:
        result = _documents_table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            return None, _response(404, {"message": f"Document not found: {document_id}"})
        if item.get("course_id") != course_id:
            return None, _response(403, {"message": f"Forbidden for document: {document_id}"})
        processing_status = str(item.get("processing_status") or "").strip().upper()
        if processing_status == "GENERATING":
            return None, _response(409, {"message": "Quiz generation already in progress"})
        processed_key = item.get("s3_processed_key")
        if not processed_key:
            return None, _response(400, {"message": f"Document is not processed yet: {document_id}"})
        source_keys[document_id] = processed_key
    logger.info("cid=%s validated_documents=%s", correlation_id, len(document_ids))
    return source_keys, None


def _rollback_documents_ready(document_ids, correlation_id):
    for document_id in document_ids:
        try:
            _documents_table.update_item(
                Key={"document_id": document_id},
                UpdateExpression="SET processing_status = :ready",
                ExpressionAttributeValues={":ready": "READY"},
            )
        except ClientError:
            logger.warning("cid=%s rollback_ready_failed doc=%s", correlation_id, document_id)


def _claim_documents_for_quiz(document_ids, correlation_id):
    """READY -> GENERATING with conditional updates; rollback prior claims on any failure."""
    claimed = []
    for document_id in document_ids:
        try:
            _documents_table.update_item(
                Key={"document_id": document_id},
                UpdateExpression="SET processing_status = :g",
                ConditionExpression=(
                    "attribute_exists(s3_processed_key) AND "
                    "(processing_status = :ready OR processing_status = :failed)"
                ),
                ExpressionAttributeValues={
                    ":g": "GENERATING",
                    ":ready": "READY",
                    ":failed": "FAILED",
                },
            )
            claimed.append(document_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                _rollback_documents_ready(claimed, correlation_id)
                raise
            _rollback_documents_ready(claimed, correlation_id)
            logger.info(
                "cid=%s claim_generating_failed doc=%s prior_claimed=%s",
                correlation_id,
                document_id,
                len(claimed),
            )
            return (
                None,
                _response(
                    409,
                    {"message": "Quiz generation already in progress or documents not ready"},
                ),
            )
    return (claimed, None)


def _set_documents_status(document_ids, status, correlation_id):
    for document_id in document_ids:
        _documents_table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET processing_status = :status",
            ExpressionAttributeValues={":status": status},
        )
    logger.info("cid=%s updated_documents_status status=%s count=%s", correlation_id, status, len(document_ids))


def _mark_quiz_generated(document_ids, correlation_id):
    for document_id in document_ids:
        _documents_table.update_item(
            Key={"document_id": document_id},
            UpdateExpression=(
                "SET processing_status = :ready, has_generated_quiz = :has_quiz "
                "REMOVE failure_reason"
            ),
            ExpressionAttributeValues={":ready": "READY", ":has_quiz": True},
        )
    logger.info(
        "cid=%s marked_documents_practiced status=READY count=%s",
        correlation_id,
        len(document_ids),
    )


def _mark_quiz_failed(document_ids, reason, correlation_id):
    short_reason = (reason or "")[:900]
    for document_id in document_ids:
        _documents_table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET processing_status = :status, failure_reason = :reason",
            ExpressionAttributeValues={
                ":status": "FAILED",
                ":reason": short_reason,
            },
        )
    logger.info(
        "cid=%s marked_documents_failed count=%s",
        correlation_id,
        len(document_ids),
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


def _generate_questions_worker(
    course_id,
    document_ids,
    correlation_id,
    requested_question_count,
    quiz_language,
    requested_by=None,
    requested_focus_weak_topics=False,
):
    topic_lists = []
    source_texts = []
    empty_text_document_ids = []
    source_document_names = []
    for document_id in document_ids:
        result = _documents_table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            raise ValueError(f"Document not found: {document_id}")
        processed_key = item.get("s3_processed_key")
        if not processed_key:
            raise ValueError(f"Document is not processed yet: {document_id}")

        item = ensure_document_topics(dict(item))
        topic_lists.append(item["topics"])
        source_document_names.append(
            item.get("original_file_name")
            or item.get("originalFileName")
            or f"Document {len(source_document_names) + 1}"
        )

        s3_obj = _s3.get_object(Bucket=PROCESSED_BUCKET, Key=processed_key)
        source_text = s3_obj["Body"].read().decode("utf-8", errors="replace")
        if not source_text.strip():
            empty_text_document_ids.append(document_id)
        source_texts.append(source_text)

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

    if empty_text_document_ids:
        logger.warning("WARNING: Extracted text is empty for documents: %s", empty_text_document_ids)

    input_text, budgets = _build_balanced_context(source_texts)
    input_text = truncate_source_text(input_text, QUIZ_MAX_SOURCE_CHARS)
    logger.info(
        "cid=%s built_balanced_context documents=%s total_input_len=%s budgets=%s",
        correlation_id,
        len(document_ids),
        len(input_text),
        budgets,
    )

    user_content = input_text
    if len(input_text) < _SHORT_SOURCE_HINT_THRESHOLD:
        user_content = (
            f"{input_text}\n\n"
            "Note: source text is limited; prioritize faithful coverage of the "
            "provided material over novelty."
        )

    _, model_name = openai_config()
    client = get_openai_client()
    completion = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": _build_system_prompt(
                    allowed_en,
                    requested_question_count,
                    quiz_language,
                    prioritized_weak_topics=(
                        prioritized_weak_topics if applied_focus_weak_topics else None
                    ),
                ),
            },
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        timeout=60,
        response_format={
            "type": "json_schema",
            "json_schema": _build_question_response_schema(
                allowed_en, requested_question_count
            ),
        },
    )
    raw_response = completion.choices[0].message.content or ""
    logger.info(
        "cid=%s openai_response_chars=%s",
        correlation_id,
        len(raw_response),
    )
    valid_questions, discarded_count, cleaned_response = _parse_valid_questions(
        raw_response, canonical_lookup=canonical_lookup
    )

    if not valid_questions:
        logger.error(
            "cid=%s no_valid_questions raw_response=%s cleaned_response=%s",
            correlation_id,
            _truncate_for_log(raw_response),
            _truncate_for_log(cleaned_response),
        )
        raise ValueError("AI response contained no valid questions")

    if len(valid_questions) != requested_question_count:
        raise ValueError(
            f"Expected {requested_question_count} valid questions, got {len(valid_questions)}"
        )

    set_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    question_count = len(valid_questions)
    default_set_name = _default_set_name(created_at)
    set_item = {
        "set_id": set_id,
        "document_ids": document_ids,
        "source_document_names": source_document_names,
        "course_id": course_id,
        "name": default_set_name,
        "set_name": default_set_name,
        "question_count": question_count,
        "quiz_language": quiz_language,
        "requested_question_count": requested_question_count,
        "difficulty_breakdown": _difficulty_breakdown(valid_questions),
        "title": f"Combined Quiz - {len(document_ids)} Materials",
        "created_at": created_at,
        **_question_set_generation_metadata(
            applied_focus_weak_topics, prioritized_weak_topics
        ),
    }
    _question_sets_table.put_item(Item=set_item)

    with _questions_table.batch_writer() as batch:
        for question in valid_questions:
            batch.put_item(
                Item={
                    "question_id": str(uuid4()),
                    "set_id": set_id,
                    "question": question["question"],
                    "options": question["options"],
                    "correct_index": question["correct_index"],
                    "explanation": question["explanation"],
                    "topics": question["topics"],
                    "difficulty": question["difficulty"],
                }
            )

    _mark_quiz_generated(document_ids, correlation_id)
    logger.info(
        "cid=%s persisted_question_set set_id=%s inserted=%s discarded=%s",
        correlation_id,
        set_id,
        question_count,
        discarded_count,
    )


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
        "cid=%s worker_start course_id=%s doc_count=%s",
        correlation_id,
        course_id,
        len(document_ids),
    )
    if requested_by:
        course_row = _courses_table.get_item(Key={"course_id": course_id}).get("Item") or {}
        if course_row.get("owner_id") != requested_by:
            logger.warning(
                "cid=%s worker_rejected owner_mismatch course_id=%s",
                correlation_id,
                course_id,
            )
            _mark_quiz_failed(document_ids, "Unauthorized", correlation_id)
            return {"ok": False}
    try:
        _generate_questions_worker(
            course_id,
            document_ids,
            correlation_id,
            requested_question_count,
            quiz_language,
            requested_by=requested_by,
            requested_focus_weak_topics=requested_focus_weak_topics,
        )
    except Exception as exc:
        logger.exception("cid=%s worker_failed course_id=%s", correlation_id, course_id)
        _mark_quiz_failed(document_ids, str(exc)[:500], correlation_id)
        return {"ok": False}
    return {"ok": True}


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

        _, error_response = _validate_documents(course_id, document_ids, correlation_id)
        if error_response:
            return error_response

        _, claim_error = _claim_documents_for_quiz(document_ids, correlation_id)
        if claim_error:
            return claim_error

        worker_payload = {
            "courseId": course_id,
            "documentIds": document_ids,
            "requestedBy": requested_by,
            "apiRequestId": correlation_id,
            "requestedQuestionCount": parsed["requested_question_count"],
            "quizLanguage": parsed["quiz_language"],
            "focusWeakTopics": parsed["focus_weak_topics"],
        }
        try:
            logger.info("cid=%s invoking_worker", correlation_id)
            _invoke_worker_async(worker_payload, correlation_id)
        except Exception:
            logger.exception("cid=%s failed_to_enqueue_worker", correlation_id)
            _set_documents_status(document_ids, "READY", correlation_id)
            return _response(500, {"message": "Failed to start async generation job"})

        return _response(
            202,
            {
                "message": "Question generation started",
                "course_id": course_id,
                "documents_queued": len(document_ids),
                "request_id": correlation_id,
            },
        )
    except Exception as exc:
        logger.exception("cid=%s unhandled_error", correlation_id)
        return _response(500, {"message": "Internal server error", "error": str(exc)})
