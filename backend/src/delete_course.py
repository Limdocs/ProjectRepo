import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key

COURSES_TABLE = os.environ["COURSES_TABLE"]
DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
QUESTION_SETS_TABLE = os.environ["QUESTION_SETS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
ATTEMPTS_TABLE = os.environ["ATTEMPTS_TABLE"]
ATTEMPT_ANSWERS_TABLE = os.environ["ATTEMPT_ANSWERS_TABLE"]
USER_PROGRESS_TABLE = os.environ["USER_PROGRESS_TABLE"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
INDEX_NAME = os.environ["INDEX_NAME"]
QUESTION_SETS_COURSE_INDEX = os.environ.get("QUESTION_SETS_COURSE_INDEX", "CourseIdCreatedAtIndex")
QUESTIONS_SET_INDEX = os.environ.get("QUESTIONS_SET_INDEX", "SetIdIndex")

_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_documents_table = _dynamodb.Table(DOCUMENTS_TABLE)
_question_sets_table = _dynamodb.Table(QUESTION_SETS_TABLE)
_questions_table = _dynamodb.Table(QUESTIONS_TABLE)
_attempts_table = _dynamodb.Table(ATTEMPTS_TABLE)
_attempt_answers_table = _dynamodb.Table(ATTEMPT_ANSWERS_TABLE)
_user_progress_table = _dynamodb.Table(USER_PROGRESS_TABLE)
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
        logger.warning(
            "Failed deleting S3 object bucket=%s key_len=%s error=%s",
            bucket,
            len(key),
            str(exc),
        )


def _query_all_documents(course_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "IndexName": INDEX_NAME,
            "KeyConditionExpression": Key("course_id").eq(course_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _documents_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def _query_all_sets(course_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "IndexName": QUESTION_SETS_COURSE_INDEX,
            "KeyConditionExpression": Key("course_id").eq(course_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = _question_sets_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


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


def _query_attempts_for_course(user_sub, course_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "KeyConditionExpression": Key("user_name").eq(user_sub),
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


def _delete_document(doc):
    document_id = doc.get("document_id")
    if not document_id:
        raise ValueError("invalid document metadata: missing document_id")
    _delete_object_safe(UPLOAD_BUCKET, doc.get("s3_raw_key"))
    _delete_object_safe(PROCESSED_BUCKET, doc.get("s3_processed_key"))
    _documents_table.delete_item(Key={"document_id": document_id})


def _delete_question_set(set_id):
    questions = _query_questions_by_set_id(set_id)
    with _questions_table.batch_writer() as batch:
        for question in questions:
            question_id = question.get("question_id")
            if question_id:
                batch.delete_item(Key={"question_id": question_id})
    _question_sets_table.delete_item(Key={"set_id": set_id})
    return len(questions)


def _delete_attempt_row(user_sub, attempt_item):
    attempt_id = attempt_item.get("attempt_id")
    submitted_at = attempt_item.get("submitted_at")
    if not attempt_id or not submitted_at:
        raise ValueError("invalid attempt metadata: missing attempt_id or submitted_at")
    answer_rows = _query_answers_for_attempt(attempt_id)
    with _attempt_answers_table.batch_writer() as batch:
        for row in answer_rows:
            question_id = row.get("question_id")
            if question_id:
                batch.delete_item(Key={"attempt_id": attempt_id, "question_id": question_id})
    _attempts_table.delete_item(Key={"user_name": user_sub, "submitted_at": submitted_at})
    return len(answer_rows)


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

        course_id = (event.get("pathParameters") or {}).get("courseId")
        if not course_id:
            return _response(400, {"message": "Missing path parameter: courseId"})

        course_result = _courses_table.get_item(Key={"course_id": course_id})
        course_item = course_result.get("Item")
        if not course_item:
            return _response(404, {"message": "Course not found"})

        if course_item.get("owner_id") != sub:
            return _response(403, {"message": "Forbidden"})

        documents = _query_all_documents(course_id)
        for doc in documents:
            _delete_document(doc)

        sets = _query_all_sets(course_id)
        deleted_sets = 0
        for set_item in sets:
            set_id = set_item.get("set_id")
            if not set_id:
                return _response(
                    500,
                    {"message": "Failed to delete course assets due to invalid question set metadata"},
                )
            _delete_question_set(set_id)
            deleted_sets += 1

        attempts = _query_attempts_for_course(sub, course_id)
        deleted_attempts = 0
        for attempt_item in attempts:
            _delete_attempt_row(sub, attempt_item)
            deleted_attempts += 1

        _user_progress_table.delete_item(Key={"user_name": sub, "course_id": course_id})

        _courses_table.delete_item(Key={"course_id": course_id})
        return _response(
            200,
            {
                "message": "Course deleted successfully",
                "deleted_documents": len(documents),
                "deleted_sets": deleted_sets,
                "deleted_attempts": deleted_attempts,
            },
        )
    except ValueError as exc:
        return _response(500, {"message": str(exc)})
    except Exception as exc:
        return _response(500, {"message": "Internal server error", "error": str(exc)})
