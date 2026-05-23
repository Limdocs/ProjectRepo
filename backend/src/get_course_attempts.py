import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from course_access import require_course_owner

COURSES_TABLE = os.environ["COURSES_TABLE"]
ATTEMPTS_TABLE = os.environ["ATTEMPTS_TABLE"]
_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_attempts_table = _dynamodb.Table(ATTEMPTS_TABLE)

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


def _serialize_attempt(item):
    score = item.get("score")
    if isinstance(score, Decimal):
        score = _json_default(score)
    time_spent = item.get("time_spent_seconds")
    if time_spent is not None:
        try:
            time_spent = int(time_spent)
        except (TypeError, ValueError):
            time_spent = None
    return {
        "attempt_id": item.get("attempt_id"),
        "course_id": item.get("course_id"),
        "question_set_id": item.get("question_set_id"),
        "score": score,
        "time_spent_seconds": time_spent,
        "submitted_at": item.get("submitted_at"),
    }


def _query_attempts_for_course(user_sub, course_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "KeyConditionExpression": Key("user_name").eq(user_sub),
            "ScanIndexForward": False,
            "FilterExpression": Attr("course_id").eq(course_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _attempts_table.query(**query_args)
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

        course_id = (event.get("pathParameters") or {}).get("courseId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})

        gate = require_course_owner(_courses_table, course_id, user_sub)
        if gate:
            status, body = gate
            return _response(status, body)

        raw_items = _query_attempts_for_course(user_sub, course_id)
        attempts = [_serialize_attempt(item) for item in raw_items]
        return _response(200, {"attempts": attempts})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
