import json
import logging
import os
import re
import time
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from limits import MAX_UPLOAD_BYTES

DOCUMENTS_TABLE = os.environ["DOCUMENTS_TABLE"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]

_SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpeg", ".jpg"}
_SKIP_TEXTRACT_STATUSES = frozenset(
    {"PROCESSING", "EXTRACTED", "GENERATED", "READY", "FAILED", "GENERATING", "ERROR"}
)
_BASENAME_DOC_ID_PATTERN = re.compile(
    r"^(?P<document_id>[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-"
    r"[0-9a-fA-F]{12})_.+"
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")
_textract = boto3.client("textract")
_dynamodb = boto3.resource("dynamodb")
_documents_table = _dynamodb.Table(DOCUMENTS_TABLE)


def _extract_document_id(source_key):
    basename = source_key.split("/")[-1]
    match = _BASENAME_DOC_ID_PATTERN.match(basename)
    if match:
        return match.group("document_id")

    raise ValueError("Could not extract document_id from key basename")


def _mark_document_failed(document_id, reason):
    if not document_id:
        return
    short_reason = (reason or "")[:900]
    try:
        _documents_table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET processing_status = :status, failure_reason = :reason",
            ExpressionAttributeValues={
                ":status": "FAILED",
                ":reason": short_reason,
            },
        )
    except ClientError:
        logger.exception("Failed to mark document_id=%s as FAILED", document_id)


def _success_response(message, **extra):
    body = {"message": message, **extra}
    return {"statusCode": 200, "body": json.dumps(body)}


def _collect_textract_lines(job_id):
    all_lines = []
    next_token = None

    while True:
        response = (
            _textract.get_document_text_detection(JobId=job_id, NextToken=next_token)
            if next_token
            else _textract.get_document_text_detection(JobId=job_id)
        )

        status = response.get("JobStatus")
        if status in ("FAILED", "PARTIAL_SUCCESS"):
            raise RuntimeError(f"Textract job failed with status: {status}")

        if status != "SUCCEEDED":
            logger.info("Textract job %s status: %s. Retrying in 5s.", job_id, status)
            time.sleep(5)
            continue

        blocks = response.get("Blocks", [])
        for block in blocks:
            if block.get("BlockType") == "LINE" and block.get("Text"):
                all_lines.append(block["Text"])

        next_token = response.get("NextToken")
        if not next_token:
            break

    return all_lines


def lambda_handler(event, context):
    del context
    try:
        record = event["Records"][0]["s3"]
        source_bucket = record["bucket"]["name"]
        source_key = unquote_plus(record["object"]["key"])
        key_tail = source_key.split("/")[-1] if "/" in source_key else source_key
        logger.info(
            "S3 ObjectCreated bucket=%s key_tail_len=%s",
            source_bucket,
            len(key_tail),
        )

        try:
            document_id = _extract_document_id(source_key)
        except ValueError:
            logger.info("Skipping object: could not parse document_id from key")
            return _success_response("Ignored: key does not match document pattern")

        result = _documents_table.get_item(Key={"document_id": document_id})
        item = result.get("Item")
        if not item:
            logger.info("No DynamoDB row for document_id=%s; skipping", document_id)
            return _success_response("Ignored: document not registered", document_id=document_id)

        status_raw = str(item.get("processing_status") or "").strip().upper()
        if status_raw in _SKIP_TEXTRACT_STATUSES:
            logger.info(
                "Skip Textract document_id=%s status=%s",
                document_id,
                status_raw,
            )
            return _success_response("Skipped: terminal or in-flight status", document_id=document_id)

        if status_raw != "UPLOADED":
            logger.info("Skip Textract document_id=%s unexpected_status=%s", document_id, status_raw)
            return _success_response("Skipped: not UPLOADED", document_id=document_id)

        extension = os.path.splitext(source_key)[1].lower()
        if extension not in _SUPPORTED_EXTENSIONS:
            reason = f"Unsupported file extension '{extension}'"
            _mark_document_failed(document_id, reason)
            return _success_response(
                "Unsupported file type. Marked as FAILED.",
                document_id=document_id,
            )

        bucket_for_head = UPLOAD_BUCKET or source_bucket
        try:
            head = _s3.head_object(Bucket=bucket_for_head, Key=source_key)
            size = int(head.get("ContentLength") or 0)
        except ClientError as exc:
            logger.warning(
                "head_object failed document_id=%s code=%s",
                document_id,
                exc.response.get("Error", {}).get("Code"),
            )
            _mark_document_failed(document_id, "Could not read object metadata for size check")
            return _success_response("Marked FAILED: head_object error", document_id=document_id)

        if size > MAX_UPLOAD_BYTES:
            _mark_document_failed(
                document_id,
                f"File too large ({size} bytes; max {MAX_UPLOAD_BYTES})",
            )
            return _success_response("Marked FAILED: file too large", document_id=document_id)

        try:
            _documents_table.update_item(
                Key={"document_id": document_id},
                UpdateExpression="SET processing_status = :processing",
                ConditionExpression="attribute_exists(document_id) AND processing_status = :uploaded",
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":uploaded": "UPLOADED",
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.info(
                    "Claim lost or status changed document_id=%s (no Textract)",
                    document_id,
                )
                return _success_response("Skipped: claim not acquired", document_id=document_id)
            raise

        start_response = _textract.start_document_text_detection(
            DocumentLocation={
                "S3Object": {
                    "Bucket": source_bucket,
                    "Name": source_key,
                }
            }
        )
        job_id = start_response["JobId"]
        logger.info("Started Textract job %s document_id=%s", job_id, document_id)

        extracted_lines = _collect_textract_lines(job_id)
        extracted_text = "\n".join(extracted_lines)

        processed_key = f"extracted_text/{document_id}.txt"
        _s3.put_object(
            Bucket=PROCESSED_BUCKET,
            Key=processed_key,
            Body=extracted_text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

        logger.info("Updating document READY document_id=%s", document_id)
        _documents_table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET processing_status = :s, s3_processed_key = :k REMOVE failure_reason",
            ConditionExpression="attribute_exists(document_id)",
            ExpressionAttributeValues={
                ":s": "READY",
                ":k": processed_key,
            },
        )

        return _success_response(
            "Document processed successfully",
            document_id=document_id,
            processed_key=processed_key,
        )
    except Exception as exc:
        doc_for_log = locals().get("document_id")
        logger.exception("Failed processing document_id=%s", doc_for_log)
        if doc_for_log:
            try:
                _mark_document_failed(doc_for_log, str(exc)[:500])
            except Exception:
                logger.exception("Failed to update DynamoDB failed status")
        raise
