import base64
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from course_access import require_course_owner

COURSES_TABLE = os.environ["COURSES_TABLE"]
QUESTION_SETS_TABLE = os.environ["QUESTION_SETS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
ATTEMPTS_TABLE = os.environ["ATTEMPTS_TABLE"]
ATTEMPT_ANSWERS_TABLE = os.environ["ATTEMPT_ANSWERS_TABLE"]
USER_PROGRESS_TABLE = os.environ["USER_PROGRESS_TABLE"]
QUESTIONS_SET_INDEX = os.environ.get("QUESTIONS_SET_INDEX", "SetIdIndex")

_ALLOWED_DIFFICULTIES = {"Easy", "Medium", "Hard"}
_FALLBACK_TOPIC = "Uncategorized"

_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_question_sets_table = _dynamodb.Table(QUESTION_SETS_TABLE)
_questions_table = _dynamodb.Table(QUESTIONS_TABLE)
_attempts_table = _dynamodb.Table(ATTEMPTS_TABLE)
_attempt_answers_table = _dynamodb.Table(ATTEMPT_ANSWERS_TABLE)
_user_progress_table = _dynamodb.Table(USER_PROGRESS_TABLE)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"
_ALLOW_METHODS = "POST,OPTIONS"


def _cors_headers(allow_methods):
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": allow_methods,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
    }


def _response(status_code, payload, allow_methods=_ALLOW_METHODS):
    def _json_default(value):
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    return {
        "statusCode": status_code,
        "headers": _cors_headers(allow_methods),
        "body": json.dumps(payload, ensure_ascii=False, default=_json_default),
    }


def _claim_sub(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
        .get("sub")
    )


def _get_path_param(event, key):
    return (event.get("pathParameters") or {}).get(key)


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_difficulty(value):
    normalized = str(value or "").strip().title()
    if normalized in _ALLOWED_DIFFICULTIES:
        return normalized
    return "Medium"


def _normalize_topics(value):
    if not isinstance(value, list):
        return [_FALLBACK_TOPIC]

    seen = set()
    topics = []
    for item in value:
        if not isinstance(item, str):
            continue
        topic = item.strip()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        topics.append(topic)

    return topics or [_FALLBACK_TOPIC]


def _get_set_or_404(course_id, set_id):
    result = _question_sets_table.get_item(Key={"set_id": set_id})
    item = result.get("Item")
    if not item or item.get("course_id") != course_id:
        return None
    return item


def _query_questions_by_set_id(set_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "IndexName": QUESTIONS_SET_INDEX,
            "KeyConditionExpression": Key("set_id").eq(set_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _questions_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def _parse_body(event):
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded", False):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    return json.loads(raw_body)


def _is_answer_correct(user_answer, correct_index):
    if user_answer is None:
        return False
    try:
        return int(user_answer) == int(correct_index)
    except (TypeError, ValueError):
        return False


def _calculate_score(questions, answers):
    total = len(questions)
    if total == 0:
        return 0.0, []

    graded_rows = []
    correct_count = 0
    for question in questions:
        question_id = question.get("question_id")
        if not question_id:
            continue
        correct_index = _safe_int(question.get("correct_index"))
        user_answer = answers.get(question_id)
        is_correct = _is_answer_correct(user_answer, correct_index)
        if is_correct:
            correct_count += 1
        graded_rows.append(
            {
                "question_id": question_id,
                "user_answer": user_answer,
                "is_correct": is_correct,
                "feedback_explanation": question.get("explanation") or "",
            }
        )

    score_float = round((correct_count / total) * 100, 2)
    return score_float, graded_rows


def _new_delta_bucket():
    return {"total": 0, "correct": 0}


def _accumulate_matrix_deltas(questions, answers, *, user_name, course_id):
    deltas = defaultdict(lambda: defaultdict(_new_delta_bucket))

    for question in questions:
        question_id = question.get("question_id")
        if not question_id:
            continue

        correct_index = _safe_int(question.get("correct_index"))
        user_answer = answers.get(question_id)
        is_correct = _is_answer_correct(user_answer, correct_index)
        difficulty = _normalize_difficulty(question.get("difficulty"))

        for topic in _normalize_topics(question.get("topics")):
            logger.info(
                "user=%s course=%s updating matrix for topic=%s difficulty=%s correct=%s",
                user_name,
                course_id,
                topic,
                difficulty,
                is_correct,
            )
            bucket = deltas[topic][difficulty]
            bucket["total"] += 1
            if is_correct:
                bucket["correct"] += 1

    return deltas


def _matrix_names_root():
    return {"#matrix": "matrix"}


def _matrix_names_topic(topic):
    return {"#matrix": "matrix", "#topic": topic}


def _matrix_names_topic_diff(topic, difficulty):
    return {"#matrix": "matrix", "#topic": topic, "#diff": difficulty}


def _matrix_names_increment(topic, difficulty):
    return {
        "#matrix": "matrix",
        "#topic": topic,
        "#diff": difficulty,
        "#total": "total",
        "#correct": "correct",
    }


def _matrix_key(*, user_name, course_id):
    return {"user_name": user_name, "course_id": course_id}


def _try_increment_matrix_cell(
    *, user_name, course_id, topic, difficulty, total_delta, correct_delta
):
    _user_progress_table.update_item(
        Key=_matrix_key(user_name=user_name, course_id=course_id),
        UpdateExpression=(
            "ADD #matrix.#topic.#diff.#total :total_inc, "
            "#matrix.#topic.#diff.#correct :correct_inc"
        ),
        ExpressionAttributeNames=_matrix_names_increment(topic, difficulty),
        ExpressionAttributeValues={
            ":total_inc": total_delta,
            ":correct_inc": correct_delta,
        },
    )


def _initialize_matrix_path(*, user_name, course_id, topic, difficulty):
    key = _matrix_key(user_name=user_name, course_id=course_id)
    zero = Decimal(0)

    _user_progress_table.update_item(
        Key=key,
        UpdateExpression="SET #matrix = if_not_exists(#matrix, :empty_map)",
        ExpressionAttributeNames=_matrix_names_root(),
        ExpressionAttributeValues={":empty_map": {}},
    )
    _user_progress_table.update_item(
        Key=key,
        UpdateExpression="SET #matrix.#topic = if_not_exists(#matrix.#topic, :empty_topic)",
        ExpressionAttributeNames=_matrix_names_topic(topic),
        ExpressionAttributeValues={":empty_topic": {}},
    )
    _user_progress_table.update_item(
        Key=key,
        UpdateExpression=(
            "SET #matrix.#topic.#diff = if_not_exists(#matrix.#topic.#diff, :empty_cell)"
        ),
        ExpressionAttributeNames=_matrix_names_topic_diff(topic, difficulty),
        ExpressionAttributeValues={
            ":empty_cell": {"correct": zero, "total": zero},
        },
    )


def _increment_matrix_cell(
    *, user_name, course_id, topic, difficulty, total_delta, correct_delta
):
    if total_delta <= 0:
        return
    if correct_delta < 0 or correct_delta > total_delta:
        raise ValueError("correct_delta must be within [0, total_delta]")

    try:
        _try_increment_matrix_cell(
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
            total_delta=total_delta,
            correct_delta=correct_delta,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationException":
            raise
        logger.info(
            "matrix path missing; initializing user=%s course=%s topic=%s difficulty=%s",
            user_name,
            course_id,
            topic,
            difficulty,
        )
        _initialize_matrix_path(
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
        )
        _try_increment_matrix_cell(
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
            total_delta=total_delta,
            correct_delta=correct_delta,
        )


def _apply_matrix_updates(*, user_name, course_id, deltas):
    for topic, by_difficulty in deltas.items():
        for difficulty, bucket in by_difficulty.items():
            total_delta = int(bucket["total"])
            correct_delta = int(bucket["correct"])
            if total_delta <= 0:
                continue
            _increment_matrix_cell(
                user_name=user_name,
                course_id=course_id,
                topic=topic,
                difficulty=difficulty,
                total_delta=total_delta,
                correct_delta=correct_delta,
            )


def lambda_handler(event, _context):
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"})

        if method != "POST":
            return _response(405, {"message": "Method not allowed"})

        user_sub = _claim_sub(event)
        if not user_sub:
            return _response(401, {"message": "Unauthorized: missing user identity"})

        course_id = _get_path_param(event, "courseId")
        set_id = _get_path_param(event, "setId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})
        if not set_id:
            return _response(400, {"message": "Missing path parameter: setId"})

        gate = require_course_owner(_courses_table, course_id, user_sub)
        if gate:
            status, body = gate
            return _response(status, body)

        try:
            body = _parse_body(event)
        except json.JSONDecodeError:
            return _response(400, {"message": "Invalid JSON in request body"})

        if "time_spent_seconds" not in body:
            return _response(400, {"message": "Field 'time_spent_seconds' is required"})
        if "answers" not in body:
            return _response(400, {"message": "Field 'answers' is required"})

        time_spent_seconds = _safe_int(body.get("time_spent_seconds"), default=-1)
        if time_spent_seconds < 0:
            return _response(400, {"message": "Field 'time_spent_seconds' must be a non-negative integer"})

        answers = body.get("answers")
        if not isinstance(answers, dict):
            return _response(400, {"message": "Field 'answers' must be an object"})

        if not _get_set_or_404(course_id, set_id):
            return _response(404, {"message": "Question set not found"})

        try:
            questions = _query_questions_by_set_id(set_id)
        except ClientError:
            logger.exception(
                "submit_attempt query failed course_id=%s set_id=%s",
                course_id,
                set_id,
            )
            return _response(500, {"message": "Internal server error"})

        score_float, graded_rows = _calculate_score(questions, answers)
        matrix_deltas = _accumulate_matrix_deltas(
            questions, answers, user_name=user_sub, course_id=course_id
        )
        score_for_dynamo = Decimal(str(score_float))

        attempt_id = str(uuid4())
        submitted_at = datetime.now(timezone.utc).isoformat()

        _attempts_table.put_item(
            Item={
                "user_name": user_sub,
                "submitted_at": submitted_at,
                "attempt_id": attempt_id,
                "course_id": course_id,
                "question_set_id": set_id,
                "score": score_for_dynamo,
                "time_spent_seconds": time_spent_seconds,
            }
        )

        if graded_rows:
            with _attempt_answers_table.batch_writer() as batch:
                for row in graded_rows:
                    batch.put_item(
                        Item={
                            "attempt_id": attempt_id,
                            "question_id": row["question_id"],
                            "user_answer": row["user_answer"],
                            "is_correct": row["is_correct"],
                            "feedback_explanation": row["feedback_explanation"],
                        }
                    )

        try:
            _apply_matrix_updates(
                user_name=user_sub, course_id=course_id, deltas=matrix_deltas
            )
        except Exception:
            logger.exception(
                "submit_attempt matrix update failed attempt_id=%s course_id=%s user=%s",
                attempt_id,
                course_id,
                user_sub,
            )
            return _response(500, {"message": "Internal server error"})

        return _response(
            200,
            {
                "attempt_id": attempt_id,
                "score": score_float,
            },
        )
    except Exception as exc:
        logger.exception(
            "submit_attempt error course_id=%s set_id=%s",
            _get_path_param(event, "courseId"),
            _get_path_param(event, "setId"),
        )
        return _response(500, {"message": "Internal server error", "error": str(exc)})
