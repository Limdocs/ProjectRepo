import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from course_access import require_course_owner
from progress_matrix import (
    MatrixDriftError,
    apply_negative_matrix_deltas,
    rebuild_user_progress_matrix,
    resolve_attempt_deltas,
    utc_now_iso,
)

COURSES_TABLE = os.environ["COURSES_TABLE"]
ATTEMPTS_TABLE = os.environ["ATTEMPTS_TABLE"]
ATTEMPT_ANSWERS_TABLE = os.environ["ATTEMPT_ANSWERS_TABLE"]
USER_PROGRESS_TABLE = os.environ["USER_PROGRESS_TABLE"]
QUESTIONS_TABLE = os.environ["QUESTIONS_TABLE"]
QUESTIONS_SET_INDEX = os.environ.get("QUESTIONS_SET_INDEX", "SetIdIndex")

_dynamodb = boto3.resource("dynamodb")
_courses_table = _dynamodb.Table(COURSES_TABLE)
_attempts_table = _dynamodb.Table(ATTEMPTS_TABLE)
_attempt_answers_table = _dynamodb.Table(ATTEMPT_ANSWERS_TABLE)
_user_progress_table = _dynamodb.Table(USER_PROGRESS_TABLE)
_questions_table = _dynamodb.Table(QUESTIONS_TABLE)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CORS_ALLOW_HEADERS = "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"
_ALLOW_METHODS = "DELETE,OPTIONS"


def _cors_headers(allow_methods):
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": allow_methods,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
    }


def _response(status_code, payload, allow_methods=_ALLOW_METHODS):
    return {
        "statusCode": status_code,
        "headers": _cors_headers(allow_methods),
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _claim_sub(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
        .get("sub")
    )


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


def _clear_delete_lock(user_sub, submitted_at):
    try:
        _attempts_table.update_item(
            Key={"user_name": user_sub, "submitted_at": submitted_at},
            UpdateExpression="REMOVE delete_started_at",
        )
    except ClientError:
        logger.exception("failed clearing delete_started_at user=%s", user_sub)


def _acquire_delete_lock(user_sub, submitted_at):
    now = utc_now_iso()
    _attempts_table.update_item(
        Key={"user_name": user_sub, "submitted_at": submitted_at},
        UpdateExpression="SET delete_started_at = :now",
        ConditionExpression=(
            "attribute_not_exists(matrix_reverted_at) AND "
            "attribute_not_exists(delete_started_at)"
        ),
        ExpressionAttributeValues={":now": now},
    )


def _mark_matrix_reverted(user_sub, submitted_at):
    _attempts_table.update_item(
        Key={"user_name": user_sub, "submitted_at": submitted_at},
        UpdateExpression="SET matrix_reverted_at = :ts",
        ExpressionAttributeValues={":ts": utc_now_iso()},
    )


def _revert_progress_matrix(user_sub, course_id, attempt_item, answer_rows, attempt_id):
    submitted_at = attempt_item["submitted_at"]

    try:
        deltas = resolve_attempt_deltas(
            attempt_item,
            questions_table=_questions_table,
            questions_set_index=QUESTIONS_SET_INDEX,
            answer_rows=answer_rows,
        )
    except ValueError:
        logger.warning(
            "delete_attempt cannot resolve deltas attempt_id=%s course_id=%s",
            attempt_id,
            course_id,
        )
        return _response(
            409,
            {"message": "Progress cannot be reconciled for this attempt"},
        )

    if not deltas:
        _mark_matrix_reverted(user_sub, submitted_at)
        return None

    try:
        apply_negative_matrix_deltas(_user_progress_table, user_sub, course_id, deltas)
        _mark_matrix_reverted(user_sub, submitted_at)
        return None
    except MatrixDriftError:
        logger.warning(
            "delete_attempt matrix drift attempt_id=%s; rebuilding progress",
            attempt_id,
        )
    except ClientError:
        logger.exception("delete_attempt matrix subtract failed attempt_id=%s", attempt_id)
        _clear_delete_lock(user_sub, submitted_at)
        return _response(500, {"message": "Internal server error"})

    try:
        rebuild_user_progress_matrix(
            user_progress_table=_user_progress_table,
            attempts_table=_attempts_table,
            questions_table=_questions_table,
            questions_set_index=QUESTIONS_SET_INDEX,
            answers_table=_attempt_answers_table,
            user_name=user_sub,
            course_id=course_id,
            exclude_attempt_id=attempt_id,
        )
        _mark_matrix_reverted(user_sub, submitted_at)
        return None
    except Exception:
        logger.exception("delete_attempt rebuild failed attempt_id=%s", attempt_id)
        _clear_delete_lock(user_sub, submitted_at)
        return _response(
            409,
            {"message": "Progress cannot be reconciled for this attempt"},
        )


def lambda_handler(event, context):
    del context
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "OPTIONS":
            return _response(200, {"message": "OK"})

        if method != "DELETE":
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

        submitted_at = attempt_item.get("submitted_at")
        if not submitted_at:
            return _response(500, {"message": "Attempt record is missing submitted_at"})

        answer_rows = _query_answers_for_attempt(attempt_id)

        if (
            not attempt_item.get("matrix_reverted_at")
            and attempt_item.get("progress_applied_at")
        ):
            if not attempt_item.get("delete_started_at"):
                try:
                    _acquire_delete_lock(user_sub, submitted_at)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                    attempt_item = (
                        _attempts_table.get_item(
                            Key={"user_name": user_sub, "submitted_at": submitted_at}
                        ).get("Item")
                        or attempt_item
                    )
                    if attempt_item.get("matrix_reverted_at"):
                        pass
                    elif attempt_item.get("delete_started_at"):
                        return _response(
                            409,
                            {"message": "Attempt deletion already in progress"},
                        )
                    else:
                        raise

            attempt_item = (
                _attempts_table.get_item(
                    Key={"user_name": user_sub, "submitted_at": submitted_at}
                ).get("Item")
                or attempt_item
            )
            if not attempt_item.get("matrix_reverted_at"):
                revert_error = _revert_progress_matrix(
                    user_sub, course_id, attempt_item, answer_rows, attempt_id
                )
                if revert_error:
                    return revert_error

        with _attempt_answers_table.batch_writer() as batch:
            for row in answer_rows:
                question_id = row.get("question_id")
                if question_id:
                    batch.delete_item(
                        Key={"attempt_id": attempt_id, "question_id": question_id}
                    )

        _attempts_table.delete_item(
            Key={"user_name": user_sub, "submitted_at": submitted_at}
        )

        return _response(
            200,
            {
                "message": "Attempt deleted",
                "attempt_id": attempt_id,
                "deleted_answers": len(answer_rows),
            },
        )
    except Exception as exc:
        logger.exception("delete_attempt error")
        return _response(500, {"message": "Internal server error", "error": str(exc)})
