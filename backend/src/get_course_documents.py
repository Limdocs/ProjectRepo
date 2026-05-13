import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from course_access import require_course_owner

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
INDEX_NAME = os.environ["INDEX_NAME"]
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(DOCUMENTS_TABLE)
_courses_table = _dynamodb.Table(COURSES_TABLE)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"


def _cors_headers(allow_methods="GET,OPTIONS"):
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": allow_methods,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
    }


def _response(status_code, payload, allow_methods="GET,OPTIONS"):
    return {
        "statusCode": status_code,
        "headers": _cors_headers(allow_methods),
        "body": json.dumps(payload),
    }


def lambda_handler(event, context):
    del context
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"}, allow_methods="GET,OPTIONS")

        claims = (
            event.get("requestContext", {})
            .get("authorizer", {})
            .get("claims", {})
        )
        user_sub = claims.get("sub")
        if not user_sub:
            return _response(401, {"message": "Unauthorized: missing user identity"})

        course_id = event.get("pathParameters", {}).get("courseId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})

        gate = require_course_owner(_courses_table, course_id, user_sub)
        if gate:
            status, body = gate
            return _response(status, body)

        query_result = _table.query(
            IndexName=INDEX_NAME,
            KeyConditionExpression=Key("course_id").eq(course_id),
        )
        items = query_result.get("Items", [])

        return _response(200, {"documents": items})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
