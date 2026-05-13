import json
import logging
import os

import boto3

from course_access import require_course_owner

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(DOCUMENTS_TABLE)
_courses_table = _dynamodb.Table(COURSES_TABLE)
_s3 = boto3.client("s3")
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"


def _response(status_code, payload, allow_methods="DELETE,OPTIONS"):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": allow_methods,
            "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        },
        "body": json.dumps(payload),
    }


def _delete_object_safe(bucket, key):
    if not key:
        return
    try:
        logger.info("Deleting S3 object bucket=%s key_len=%s", bucket, len(key))
        _s3.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        # Missing objects should not block metadata cleanup; log and continue.
        logger.warning(
            "Failed deleting S3 object bucket=%s key_len=%s error=%s",
            bucket,
            len(key),
            str(exc),
        )


def lambda_handler(event, context):
    del context
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"})

        claims = (
            event.get("requestContext", {})
            .get("authorizer", {})
            .get("claims", {})
        )
        sub = claims.get("sub")
        if not sub:
            return _response(401, {"message": "Unauthorized: missing user identity"})

        path_parameters = event.get("pathParameters", {})
        course_id = path_parameters.get("courseId")
        document_id = path_parameters.get("documentId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})
        if not document_id:
            return _response(400, {"message": "Missing path parameter: documentId"})

        gate = require_course_owner(_courses_table, course_id, sub)
        if gate:
            status, body = gate
            return _response(status, body)

        result = _table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            return _response(404, {"message": "Document not found"})

        if item.get("course_id") != course_id:
            return _response(403, {"message": "Forbidden"})

        s3_raw_key = item.get("s3_raw_key")
        s3_processed_key = item.get("s3_processed_key")

        # Attempt storage cleanup first, then remove metadata regardless of missing objects.
        _delete_object_safe(UPLOAD_BUCKET, s3_raw_key)
        _delete_object_safe(PROCESSED_BUCKET, s3_processed_key)

        _table.delete_item(Key={"document_id": document_id})

        return _response(200, {"message": "Document deleted successfully"})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
