import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from course_access import require_course_owner

COURSES_TABLE = os.environ["COURSES_TABLE"]
ATTEMPTS_TABLE = os.environ["ATTEMPTS_TABLE"]
ATTEMPT_ANSWERS_TABLE = os.environ["ATTEMPT_ANSWERS_TABLE"]
_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_attempts_table = _dynamodb.Table(ATTEMPTS_TABLE)
_attempt_answers_table = _dynamodb.Table(ATTEMPT_ANSWERS_TABLE)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"
_ALLOW_METHODS = "GET,OPTIONS"


def _cors_headers(allow_methods):
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": allow_methods,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status_code, payload, allow_methods=_ALLOW_METHODS):
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


def _serialize_user_answer(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return _json_default(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _find_attempt_for_user(user_sub, course_id, attempt_id):
    last_evaluated_key = None
    while True:
        query_args = {
            "KeyConditionExpression": Key("user_name").eq(user_sub),
            "FilterExpression": Attr("course_id").eq(course_id) & Attr("attempt_id").eq(attempt_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _attempts_table.query(**query_args)
        items = result.get("Items", [])
        if items:
            return items[0]
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return None


def _query_answers_for_attempt(attempt_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "KeyConditionExpression": Key("attempt_id").eq(attempt_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _attempt_answers_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def lambda_handler(event, context):
    del context
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"})

        if method != "GET":
            return _response(405, {"message": "Method not allowed"})

        user_sub = _claim_sub(event)
        if not user_sub:
            return _response(401, {"message": "Unauthorized: missing user identity"})

        path_params = event.get("pathParameters") or {}
        course_id = path_params.get("courseId")
        attempt_id = path_params.get("attemptId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})
        if not attempt_id:
            return _response(400, {"message": "Missing path parameter: attemptId"})

        gate = require_course_owner(_courses_table, course_id, user_sub)
        if gate:
            status, body = gate
            return _response(status, body)

        attempt_item = _find_attempt_for_user(user_sub, course_id, attempt_id)
        if not attempt_item:
            return _response(404, {"message": "Attempt not found"})

        answer_rows = _query_answers_for_attempt(attempt_id)
        answers = {}
        for row in answer_rows:
            question_id = row.get("question_id")
            if not question_id:
                continue
            answers[str(question_id)] = _serialize_user_answer(row.get("user_answer"))

        return _response(200, {"answers": answers})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
