import json
import logging
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from course_access import require_course_owner

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
QUESTION_SETS_TABLE = os.environ["QUESTION_SETS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL_NAME = os.environ["OPENAI_MODEL_NAME"]

_MAX_SOURCE_CHARS = 12000
_ALLOWED_DIFFICULTIES = {"Easy", "Medium", "Hard"}

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

_SYSTEM_PROMPT = (
    "You are an expert academic assistant. Generate 5 high-quality multiple-choice "
    "questions in Hebrew based on the provided text. Categorize each question with "
    "relevant topics and assign difficulty as Easy, Medium, or Hard based on the "
    "academic depth of the text. Ensure at least 1-2 questions synthesize or compare "
    "information across multiple provided documents. Return ONLY a valid JSON array of objects. "
    "Each object must include: question (string), options (array of 4 strings), "
    "correct_index (integer 0-3), explanation (string), topics (array of strings), "
    "difficulty (Easy|Medium|Hard). Optionally, answer (string) may be included as "
    "redundant text matching one value in options."
)


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


def _clean_model_json(raw_text):
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return text


def _extract_json_payload(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return text

    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fenced_match:
        text = fenced_match.group(1).strip()

    text = _clean_model_json(text)
    if not text:
        return text

    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return text[start : end + 1].strip()

    return text


def _truncate_for_log(value, limit=3000):
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _truncate_source_text(text):
    if len(text) <= _MAX_SOURCE_CHARS:
        return text
    return text[:_MAX_SOURCE_CHARS]


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
    budgets = _allocate_budgets(texts, _MAX_SOURCE_CHARS)
    parts = [texts[idx][:budgets[idx]] for idx in range(len(texts)) if budgets[idx] > 0]
    return "\n\n".join(parts), budgets


def _normalize_question(item):
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
        normalized_topics = [str(t).strip() for t in topics if isinstance(t, str) and t.strip()]
    elif isinstance(topic, str) and topic.strip():
        normalized_topics = [topic.strip()]
    else:
        normalized_topics = []
    if not normalized_topics:
        return None

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
        "topics": normalized_topics,
        "difficulty": normalized_difficulty,
    }


def _parse_valid_questions(raw_response):
    cleaned = _extract_json_payload(raw_response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response is not valid JSON after cleaning: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("Model response must be a JSON array")

    valid = []
    discarded = 0
    for item in parsed:
        normalized = _normalize_question(item)
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
    return {
        "course_id": course_id,
        "document_ids": normalized_document_ids,
        "requested_by": claims["sub"],
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
                ConditionExpression="attribute_exists(s3_processed_key) AND processing_status = :ready",
                ExpressionAttributeValues={":g": "GENERATING", ":ready": "READY"},
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
            UpdateExpression="SET processing_status = :ready, has_generated_quiz = :has_quiz",
            ExpressionAttributeValues={":ready": "READY", ":has_quiz": True},
        )
    logger.info(
        "cid=%s marked_documents_practiced status=READY count=%s",
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


def _generate_questions_worker(course_id, document_ids, correlation_id):
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

    if empty_text_document_ids:
        logger.warning("WARNING: Extracted text is empty for documents: %s", empty_text_document_ids)

    input_text, budgets = _build_balanced_context(source_texts)
    input_text = _truncate_source_text(input_text)
    logger.info(
        "cid=%s built_balanced_context documents=%s total_input_len=%s budgets=%s",
        correlation_id,
        len(document_ids),
        len(input_text),
        budgets,
    )

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    completion = client.chat.completions.create(
        model=OPENAI_MODEL_NAME,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": input_text},
        ],
        temperature=0.2,
        timeout=60,
    )
    raw_response = completion.choices[0].message.content or ""
    logger.info(
        "cid=%s openai_response_chars=%s",
        correlation_id,
        len(raw_response),
    )
    valid_questions, discarded_count, cleaned_response = _parse_valid_questions(raw_response)

    if not valid_questions:
        logger.error(
            "cid=%s no_valid_questions raw_response=%s cleaned_response=%s",
            correlation_id,
            _truncate_for_log(raw_response),
            _truncate_for_log(cleaned_response),
        )
        raise ValueError("AI response contained no valid questions")

    set_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    question_count = len(valid_questions)
    default_set_name = _default_set_name(created_at)
    _question_sets_table.put_item(
        Item={
            "set_id": set_id,
            "document_ids": document_ids,
            "source_document_names": source_document_names,
            "course_id": course_id,
            "name": default_set_name,
            "set_name": default_set_name,
            "question_count": question_count,
            "difficulty_breakdown": _difficulty_breakdown(valid_questions),
            "title": f"Combined Quiz - {len(document_ids)} Materials",
            "created_at": created_at,
        }
    )

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


def _invoke_worker_async(payload, context, correlation_id):
    function_arn = context.invoked_function_arn
    if not function_arn:
        raise RuntimeError("Cannot resolve function ARN for async invocation")
    _lambda.invoke(
        FunctionName=function_arn,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    logger.info("cid=%s worker_enqueued function_arn=%s", correlation_id, function_arn)


def lambda_handler(event, context):
    correlation_id = event.get("apiRequestId") or context.aws_request_id
    try:
        if event.get("mode") == "worker":
            course_id = event.get("courseId")
            document_ids = event.get("documentIds") or []
            requested_by = event.get("requestedBy")
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
                    _set_documents_status(document_ids, "READY", correlation_id)
                    return {"ok": False}
            try:
                _generate_questions_worker(course_id, document_ids, correlation_id)
            except Exception:
                logger.exception("cid=%s worker_failed course_id=%s", correlation_id, course_id)
                _set_documents_status(document_ids, "READY", correlation_id)
                raise
            return {"ok": True}

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
            "mode": "worker",
            "courseId": course_id,
            "documentIds": document_ids,
            "requestedBy": requested_by,
            "apiRequestId": correlation_id,
        }
        try:
            logger.info("Attempting to invoke worker for CID: %s", correlation_id)
            _invoke_worker_async(worker_payload, context, correlation_id)
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
