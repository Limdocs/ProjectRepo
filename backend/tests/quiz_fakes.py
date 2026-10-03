"""In-memory DynamoDB and S3 stand-ins for quiz generation tests."""

import copy
import re
import threading

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


def client_error(code, operation="UpdateItem", reasons=None):
    response = {"Error": {"Code": code, "Message": code}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    return ClientError(response, operation)


def conditional_failed():
    return client_error("ConditionalCheckFailedException")


class QuizWorld(object):
    def __init__(self):
        self.docs = {}
        self.sets = {}
        self.questions = {}
        self.courses = {}
        self.s3 = {}
        self.calls = []
        self.invoke_error = None
        self.transact_error = None
        self.revoke_error = None
        self.lock = threading.Lock()
        self.barrier = None
        self.pair_commit = False
        self.reverse_pair = False
        self._pending_tx = []
        self._tx_results = {}
        self._pair_done = threading.Event()
        self.on_invoke = None
        self.meta = self
        self.client = self
        self._deser = TypeDeserializer()

    def bind(self, module):
        module._documents_table = self
        module._question_sets_table = self
        module._questions_table = self
        module._courses_table = self
        module._s3 = self
        module._lambda = self
        module._dynamodb = self
        module._dynamodb_transactions = self

    def add_document(self, document_id, status="READY", generation_id=None, key=None, name=None):
        item = {
            "document_id": document_id,
            "course_id": "course-1",
            "processing_status": status,
            "s3_processed_key": key or ("%s.txt" % document_id),
            "original_file_name": name or ("%s.txt" % document_id),
            "topics": [{"he": "כללי", "en": "General"}],
        }
        if generation_id is not None:
            item["generation_id"] = generation_id
        self.docs[document_id] = item
        self.s3[item["s3_processed_key"]] = "Source text for %s. " % document_id
        return item

    def get_item(self, Key, **_ignored):
        if "document_id" in Key:
            item = self.docs.get(Key["document_id"])
        elif "set_id" in Key:
            item = self.sets.get(Key["set_id"])
        elif "course_id" in Key and "user_name" not in Key:
            item = self.courses.get(Key["course_id"])
        else:
            item = None
        if not item:
            return {}
        return {"Item": copy.deepcopy(item)}

    def put_item(self, Item, ConditionExpression=None):
        self.calls.append(("put", copy.deepcopy(Item)))
        if ConditionExpression and "attribute_not_exists(set_id)" in ConditionExpression:
            if Item["set_id"] in self.sets:
                raise conditional_failed()
        self.sets[Item["set_id"]] = copy.deepcopy(Item)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ConditionExpression=None,
        **_ignored
    ):
        with self.lock:
            self.calls.append(
                (
                    "update",
                    copy.deepcopy(Key),
                    UpdateExpression,
                    copy.deepcopy(ExpressionAttributeValues),
                    ConditionExpression,
                )
            )
            values = ExpressionAttributeValues
            if "document_id" in Key:
                # Quiz generation owns generation state only. Any write to a
                # document row from this code path is the bug this fake guards.
                raise AssertionError(
                    "quiz generation wrote document %s" % Key["document_id"]
                )
            item = self.sets.get(Key["set_id"])
            if item is None:
                # update_item upserts in DynamoDB; the generation-slot row is
                # the only row created this way.
                if not self._set_condition({}, ConditionExpression, values):
                    raise conditional_failed()
                item = {"set_id": Key["set_id"]}
                self.sets[Key["set_id"]] = item
            if ":revoked" in values and self.revoke_error is not None:
                raise self.revoke_error
            if not self._set_condition(item, ConditionExpression, values):
                raise conditional_failed()
            self._apply_set(item, values)

    def _set_condition(self, item, condition, values):
        status = item.get("generation_status")
        if not condition:
            return True
        if "attribute_not_exists(active_generation_id)" in condition:
            return "active_generation_id" not in item
        if "active_generation_id = :prior" in condition:
            return item.get("active_generation_id") == values.get(":prior")
        if "generation_status IN (:pending, :generating)" in condition:
            if ":gid" in values and item.get("generation_id") != values[":gid"]:
                return False
            if status not in ("PENDING", "GENERATING"):
                return False
            if "lease_expires_at <= :now" in condition:
                return self._lease_expired(item, values)
            return True
        if "generation_status = :pending OR" in condition:
            if item.get("generation_id") != values.get(":gid"):
                return False
            if status == "PENDING":
                return True
            return status == "GENERATING" and self._lease_expired(item, values)
        if "generation_status = :from_status" in condition:
            if item.get("generation_id") != values.get(":gid"):
                return False
            if ":worker" in values and item.get("worker_request_id") != values[":worker"]:
                return False
            return status == values.get(":from_status")
        if "generation_status = :generating" in condition:
            return status == "GENERATING"
        return True

    @staticmethod
    def _lease_expired(item, values):
        lease = item.get("lease_expires_at")
        return lease is None or int(lease) <= int(values[":now"])

    def _apply_set(self, item, values):
        if ":docs" in values and ":course" in values:
            item["active_generation_id"] = values[":gid"]
            item["generation_course_id"] = values[":course"]
            item["document_ids"] = list(values[":docs"])
            return
        if ":revoked" in values:
            item["generation_status"] = values[":revoked"]
            return
        if ":aborted" in values:
            item["generation_status"] = values[":aborted"]
            item["failure_code"] = values.get(":code")
            return
        if ":failed" in values and ":from_status" in values:
            item["generation_status"] = values[":failed"]
            item["failure_code"] = values.get(":code")
            return
        if ":generating" in values and ":t" in values:
            item["generation_status"] = values[":generating"]
            item["lease_expires_at"] = values[":t"]
            item["worker_request_id"] = values.get(":w")
            item["worker_started_at"] = values.get(":started")

    def get_object(self, Bucket, Key):
        del Bucket
        if Key not in self.s3:
            raise client_error("NoSuchKey", "GetObject")
        payload = self.s3[Key].encode("utf-8")

        class _Body(object):
            def read(self_inner):
                del self_inner
                return payload

        return {"Body": _Body()}

    def invoke(self, **kwargs):
        self.calls.append(("invoke", kwargs.get("FunctionName")))
        if self.on_invoke:
            self.on_invoke()
        if self.invoke_error:
            raise self.invoke_error

    def transact_write_items(self, TransactItems, ClientRequestToken=None):
        if self.pair_commit:
            self._paired_transact(TransactItems, ClientRequestToken)
            return
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        self._apply_transact(TransactItems, ClientRequestToken)

    def _paired_transact(self, transact_items, token):
        with self.lock:
            self._pending_tx.append((token, transact_items))
            leader = len(self._pending_tx) == 2
        if leader:
            order = list(self._pending_tx)
            if self.reverse_pair:
                order = list(reversed(order))
            for item_token, items in order:
                try:
                    self._apply_transact(items, item_token)
                    self._tx_results[item_token] = None
                except ClientError as exc:
                    self._tx_results[item_token] = exc
            self._pair_done.set()
        else:
            if not self._pair_done.wait(timeout=5):
                raise RuntimeError("paired commit did not finish")
        error = self._tx_results[token]
        if error is not None:
            raise error

    def _apply_transact(self, transact_items, client_request_token):
        with self.lock:
            self.last_transact = transact_items
            self.calls.append(("transact", len(transact_items), client_request_token))
            if self.transact_error:
                raise self.transact_error
            update = [action["Update"] for action in transact_items if "Update" in action][-1]
            set_id = self._deser.deserialize(update["Key"]["set_id"])
            row = self.sets[set_id]
            if row.get("generation_status") != "GENERATING":
                reasons = [{"Code": "None"} for _ in range(len(transact_items) - 1)]
                reasons.append({"Code": "ConditionalCheckFailed"})
                raise client_error(
                    "TransactionCanceledException",
                    "TransactWriteItems",
                    reasons,
                )
            for action in transact_items:
                if "Put" not in action:
                    continue
                item = {
                    key: self._deser.deserialize(value)
                    for key, value in action["Put"]["Item"].items()
                }
                self.questions[item["question_id"]] = item
            names = update["ExpressionAttributeNames"]
            values = update["ExpressionAttributeValues"]
            for name_token, value_token in re.findall(
                r"(#f\d+) = (:f\d+)", update["UpdateExpression"]
            ):
                row[names[name_token]] = self._deser.deserialize(values[value_token])

    def Table(self, _name):
        return self
