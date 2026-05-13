import json
import logging
import os
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

from course_access import require_course_owner

QUESTION_SETS_TABLE = os.environ["QUESTION_SETS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
QUESTION_SETS_COURSE_INDEX = os.environ.get("QUESTION_SETS_COURSE_INDEX", "CourseIdCreatedAtIndex")
QUESTIONS_SET_INDEX = os.environ.get("QUESTIONS_SET_INDEX", "SetIdIndex")

_dynamodb = boto3.resource("dynamodb")
_question_sets_table = _dynamodb.Table(QUESTION_SETS_TABLE)
_questions_table = _dynamodb.Table(QUESTIONS_TABLE)
_courses_table = _dynamodb.Table(COURSES_TABLE)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"


def _cors_headers(allow_methods):
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": allow_methods,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
    }


def _response(status_code, payload, allow_methods):
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


def _get_set_or_404(course_id, set_id):
    result = _question_sets_table.get_item(Key={"set_id": set_id})
    item = result.get("Item")
    if not item or item.get("course_id") != course_id:
        return None
    return item


def _normalize_difficulty_breakdown(value):
    if not isinstance(value, dict):
        return {"easy": 0, "medium": 0, "hard": 0}
    return {
        "easy": _safe_int(value.get("easy", 0)),
        "medium": _safe_int(value.get("medium", 0)),
        "hard": _safe_int(value.get("hard", 0)),
    }


def _question_set_name(item):
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    legacy_name = item.get("set_name")
    if isinstance(legacy_name, str) and legacy_name.strip():
        return legacy_name.strip()
    return None


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_question_item(item):
    return {
        "question_id": item.get("question_id"),
        "set_id": item.get("set_id"),
        "course_id": item.get("course_id"),
        "question": item.get("question"),
        "options": item.get("options", []),
        "correct_index": _safe_int(item.get("correct_index")),
        "explanation": item.get("explanation"),
    }


def _query_questions_by_set_id(set_id):
    items = []
    last_evaluated_key = None
    try:
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
    except ClientError as exc:
        logger.warning(
            "get_questions query failed set_id=%s index=%s error=%s",
            set_id,
            QUESTIONS_SET_INDEX,
            str(exc),
        )
        raise
    return items


def _list_sets(course_id):
    try:
        result = _question_sets_table.query(
            IndexName=QUESTION_SETS_COURSE_INDEX,
            KeyConditionExpression=Key("course_id").eq(course_id),
            ScanIndexForward=False,
        )
    except ClientError as exc:
        logger.warning(
            "get_questions list_sets failed course_id=%s index=%s error=%s",
            course_id,
            QUESTION_SETS_COURSE_INDEX,
            str(exc),
        )
        raise
    items = result.get("Items", [])

    sets = []
    for item in items:
        sets.append(
            {
                "set_id": item.get("set_id"),
                "name": _question_set_name(item),
                "set_name": _question_set_name(item),
                "created_at": item.get("created_at"),
                "question_count": _safe_int(item.get("question_count", 0)),
                "difficulty_breakdown": _normalize_difficulty_breakdown(
                    item.get("difficulty_breakdown")
                ),
                "source_document_names": item.get("source_document_names", []),
                "document_ids": item.get("document_ids", []),
            }
        )

    return _response(200, {"course_id": course_id, "sets": sets}, "GET,OPTIONS")


def _get_set_details(course_id, set_id):
    set_item = _get_set_or_404(course_id, set_id)
    if not set_item:
        return _response(404, {"message": "Question set not found"}, "GET,OPTIONS")

    questions = [_normalize_question_item(item) for item in _query_questions_by_set_id(set_id)]

    return _response(
        200,
        {
            "set": {
                "set_id": set_item.get("set_id"),
                "name": _question_set_name(set_item),
                "set_name": _question_set_name(set_item),
                "created_at": set_item.get("created_at"),
                "course_id": set_item.get("course_id"),
                "question_count": _safe_int(set_item.get("question_count", 0)),
                "difficulty_breakdown": _normalize_difficulty_breakdown(
                    set_item.get("difficulty_breakdown")
                ),
                "source_document_names": set_item.get("source_document_names", []),
                "document_ids": set_item.get("document_ids", []),
            },
            "questions": questions,
        },
        "GET,OPTIONS",
    )


def _delete_set(course_id, set_id):
    set_item = _get_set_or_404(course_id, set_id)
    if not set_item:
        return _response(404, {"message": "Question set not found"}, "GET,DELETE,OPTIONS")

    questions = _query_questions_by_set_id(set_id)

    with _questions_table.batch_writer() as batch:
        for question in questions:
            question_id = question.get("question_id")
            if question_id:
                batch.delete_item(Key={"question_id": question_id})

    _question_sets_table.delete_item(Key={"set_id": set_id})

    return _response(
        200,
        {
            "message": "Question set deleted",
            "set_id": set_id,
            "deleted_questions": len(questions),
        },
        "GET,DELETE,OPTIONS",
    )


def lambda_handler(event, _context):
    method = (event.get("httpMethod") or "").upper()
    set_id = _get_path_param(event, "setId")
    route_allow_methods = "GET,DELETE,OPTIONS" if set_id else "GET,OPTIONS"

    try:
        if method == "OPTIONS":
            return _response(200, {"message": "OK"}, route_allow_methods)

        user_sub = _claim_sub(event)
        if not user_sub:
            return _response(401, {"message": "Unauthorized"}, route_allow_methods)

        course_id = _get_path_param(event, "courseId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"}, route_allow_methods)

        gate = require_course_owner(_courses_table, course_id, user_sub)
        if gate:
            status, body = gate
            return _response(status, body, route_allow_methods)

        if method == "GET" and not set_id:
            return _list_sets(course_id)
        if method == "GET" and set_id:
            return _get_set_details(course_id, set_id)
        if method == "DELETE" and set_id:
            return _delete_set(course_id, set_id)

        return _response(405, {"message": "Method not allowed"}, route_allow_methods)
    except Exception:
        logger.exception(
            "get_questions error method=%s course_id=%s set_id=%s",
            method,
            _get_path_param(event, "courseId"),
            set_id,
        )
        return _response(500, {"message": "Internal server error"}, route_allow_methods)
