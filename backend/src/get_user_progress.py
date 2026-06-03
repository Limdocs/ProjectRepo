import json
import os
from decimal import Decimal

import boto3

from course_access import require_course_owner
from topic_scoring import compute_topic_scores

COURSES_TABLE = os.environ["COURSES_TABLE"]
USER_PROGRESS_TABLE = os.environ["USER_PROGRESS_TABLE"]
_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_user_progress_table = _dynamodb.Table(USER_PROGRESS_TABLE)

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


def _extract_matrix(item):
    if not item:
        return {}
    matrix = item.get("matrix")
    if matrix is None:
        return {}
    if not isinstance(matrix, dict):
        return {}
    return matrix


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

        result = _user_progress_table.get_item(
            Key={"user_name": user_sub, "course_id": course_id}
        )
        matrix = _extract_matrix(result.get("Item"))
        topics = compute_topic_scores(matrix)
        return _response(
            200,
            {"course_id": course_id, "matrix": matrix, "topics": topics},
        )
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
