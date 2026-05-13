import base64
import json
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import boto3

from course_access import require_course_owner
from limits import MAX_UPLOAD_BYTES

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
COURSES_TABLE = os.environ["COURSES_TABLE"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(DOCUMENTS_TABLE)
_courses_table = _dynamodb.Table(COURSES_TABLE)
_s3 = boto3.client("s3")

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"


def _response(status_code, payload, allow_methods="POST,OPTIONS"):
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


def _safe_filename(name):
    if not name or not str(name).strip():
        return "file"
    base = re.sub(r"[/\\]", "", str(name).replace("\\", "/").split("/")[-1])
    return base[:500] if base else "file"


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

        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded", False):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body)

        file_name = body.get("file_name")
        file_type = body.get("file_type")
        file_size_bytes = body.get("file_size_bytes")
        if file_name is None or str(file_name).strip() == "":
            return _response(400, {"message": "Field 'file_name' is required"})
        if file_type is None or str(file_type).strip() == "":
            return _response(400, {"message": "Field 'file_type' is required"})
        try:
            size_int = int(file_size_bytes)
        except (TypeError, ValueError):
            return _response(400, {"message": "Field 'file_size_bytes' must be a non-negative integer"})
        if size_int < 0 or size_int > MAX_UPLOAD_BYTES:
            return _response(
                400,
                {"message": f"Field 'file_size_bytes' must be between 0 and {MAX_UPLOAD_BYTES}"},
            )

        safe_name = _safe_filename(file_name)
        document_id = str(uuid4())
        s3_key = f"uploads/{course_id}/{document_id}_{safe_name}"

        created_at = datetime.now(timezone.utc).isoformat()
        item = {
            "document_id": document_id,
            "course_id": course_id,
            "uploader_user_name": user_sub,
            "original_file_name": file_name,
            "file_type": file_type,
            "s3_raw_key": s3_key,
            "processing_status": "UPLOADED",
            "created_at": created_at,
        }
        _table.put_item(Item=item)

        upload_url = _s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": UPLOAD_BUCKET,
                "Key": s3_key,
                "ContentType": file_type,
            },
            ExpiresIn=3600,
        )

        return _response(
            200,
            {
                "upload_url": upload_url,
                "document_id": document_id,
                "s3_key": s3_key,
            },
        )
    except json.JSONDecodeError:
        return _response(400, {"message": "Invalid JSON in request body"})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
