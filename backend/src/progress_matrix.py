import logging
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_ALLOWED_DIFFICULTIES = {"Easy", "Medium", "Hard"}
_FALLBACK_TOPIC = "Uncategorized"


class MatrixDriftError(Exception):
    """Matrix counters would go negative after applying deltas."""


def normalize_difficulty(value):
    normalized = str(value or "").strip().title()
    if normalized in _ALLOWED_DIFFICULTIES:
        return normalized
    return "Medium"


def normalize_topics(value):
    if not isinstance(value, list):
        return [_FALLBACK_TOPIC]

    seen = set()
    topics = []
    for item in value:
        if not isinstance(item, str):
            continue
        topic = item.strip()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        topics.append(topic)

    return topics or [_FALLBACK_TOPIC]


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        if isinstance(value, Decimal):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_delta_bucket():
    return {"total": 0, "correct": 0}


def _is_answer_correct(user_answer, correct_index):
    if user_answer is None:
        return False
    try:
        return int(user_answer) == int(correct_index)
    except (TypeError, ValueError):
        return False


def build_matrix_deltas_from_questions(questions, answers, *, sign=1):
    """Build topic/difficulty delta map from questions and answer map (question_id -> answer)."""
    multiplier = 1 if sign >= 0 else -1
    deltas = defaultdict(lambda: defaultdict(_new_delta_bucket))

    for question in questions:
        question_id = question.get("question_id")
        if not question_id:
            continue

        correct_index = _safe_int(question.get("correct_index"))
        user_answer = answers.get(question_id)
        is_correct = _is_answer_correct(user_answer, correct_index)
        difficulty = normalize_difficulty(question.get("difficulty"))

        for topic in normalize_topics(question.get("topics")):
            bucket = deltas[topic][difficulty]
            bucket["total"] += multiplier
            if is_correct:
                bucket["correct"] += multiplier

    return deltas


def serialize_deltas(deltas):
    """Convert nested defaultdict deltas to a DynamoDB-friendly plain dict."""
    result = {}
    for topic, by_difficulty in deltas.items():
        topic_out = {}
        for difficulty, bucket in by_difficulty.items():
            total = _safe_int(bucket.get("total"))
            correct = _safe_int(bucket.get("correct"))
            if total == 0 and correct == 0:
                continue
            topic_out[difficulty] = {"correct": correct, "total": total}
        if topic_out:
            result[topic] = topic_out
    return result


def deserialize_deltas(raw):
    """Validate and normalize stored matrix_deltas from an attempt item."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("matrix_deltas must be an object")

    result = {}
    for topic, by_difficulty in raw.items():
        if not isinstance(topic, str) or not isinstance(by_difficulty, dict):
            continue
        topic_out = {}
        for difficulty, bucket in by_difficulty.items():
            if not isinstance(bucket, dict):
                continue
            total = _safe_int(bucket.get("total"))
            correct = _safe_int(bucket.get("correct"))
            if total == 0 and correct == 0:
                continue
            topic_out[str(difficulty)] = {"correct": correct, "total": total}
        if topic_out:
            result[topic] = topic_out
    return result


def merge_deltas(*delta_maps):
    merged = defaultdict(lambda: defaultdict(_new_delta_bucket))
    for delta_map in delta_maps:
        if not delta_map:
            continue
        for topic, by_difficulty in delta_map.items():
            for difficulty, bucket in by_difficulty.items():
                cell = merged[topic][difficulty]
                cell["total"] += _safe_int(bucket.get("total"))
                cell["correct"] += _safe_int(bucket.get("correct"))
    return merged


def negate_deltas(deltas):
    negated = defaultdict(lambda: defaultdict(_new_delta_bucket))
    for topic, by_difficulty in deltas.items():
        for difficulty, bucket in by_difficulty.items():
            negated[topic][difficulty]["total"] = -_safe_int(bucket.get("total"))
            negated[topic][difficulty]["correct"] = -_safe_int(bucket.get("correct"))
    return negated


def _matrix_key(*, user_name, course_id):
    return {"user_name": user_name, "course_id": course_id}


def _matrix_names_root():
    return {"#matrix": "matrix"}


def _matrix_names_topic(topic):
    return {"#matrix": "matrix", "#topic": topic}


def _matrix_names_topic_diff(topic, difficulty):
    return {"#matrix": "matrix", "#topic": topic, "#diff": difficulty}


def _matrix_names_increment(topic, difficulty):
    return {
        "#matrix": "matrix",
        "#topic": topic,
        "#diff": difficulty,
        "#total": "total",
        "#correct": "correct",
    }


def _try_increment_matrix_cell(table, *, user_name, course_id, topic, difficulty, total_delta, correct_delta):
    table.update_item(
        Key=_matrix_key(user_name=user_name, course_id=course_id),
        UpdateExpression=(
            "ADD #matrix.#topic.#diff.#total :total_inc, "
            "#matrix.#topic.#diff.#correct :correct_inc"
        ),
        ExpressionAttributeNames=_matrix_names_increment(topic, difficulty),
        ExpressionAttributeValues={
            ":total_inc": total_delta,
            ":correct_inc": correct_delta,
        },
    )


def _initialize_matrix_path(table, *, user_name, course_id, topic, difficulty):
    key = _matrix_key(user_name=user_name, course_id=course_id)
    zero = Decimal(0)

    table.update_item(
        Key=key,
        UpdateExpression="SET #matrix = if_not_exists(#matrix, :empty_map)",
        ExpressionAttributeNames=_matrix_names_root(),
        ExpressionAttributeValues={":empty_map": {}},
    )
    table.update_item(
        Key=key,
        UpdateExpression="SET #matrix.#topic = if_not_exists(#matrix.#topic, :empty_topic)",
        ExpressionAttributeNames=_matrix_names_topic(topic),
        ExpressionAttributeValues={":empty_topic": {}},
    )
    table.update_item(
        Key=key,
        UpdateExpression=(
            "SET #matrix.#topic.#diff = if_not_exists(#matrix.#topic.#diff, :empty_cell)"
        ),
        ExpressionAttributeNames=_matrix_names_topic_diff(topic, difficulty),
        ExpressionAttributeValues={
            ":empty_cell": {"correct": zero, "total": zero},
        },
    )


def _increment_matrix_cell(table, *, user_name, course_id, topic, difficulty, total_delta, correct_delta):
    if total_delta <= 0:
        return
    if correct_delta < 0 or correct_delta > total_delta:
        raise ValueError("correct_delta must be within [0, total_delta]")

    try:
        _try_increment_matrix_cell(
            table,
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
            total_delta=total_delta,
            correct_delta=correct_delta,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationException":
            raise
        logger.info(
            "matrix path missing; initializing user=%s course=%s topic=%s difficulty=%s",
            user_name,
            course_id,
            topic,
            difficulty,
        )
        _initialize_matrix_path(
            table,
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
        )
        _try_increment_matrix_cell(
            table,
            user_name=user_name,
            course_id=course_id,
            topic=topic,
            difficulty=difficulty,
            total_delta=total_delta,
            correct_delta=correct_delta,
        )


def apply_positive_matrix_deltas(table, user_name, course_id, deltas):
    for topic, by_difficulty in deltas.items():
        for difficulty, bucket in by_difficulty.items():
            total_delta = _safe_int(bucket.get("total"))
            correct_delta = _safe_int(bucket.get("correct"))
            if total_delta <= 0:
                continue
            _increment_matrix_cell(
                table,
                user_name=user_name,
                course_id=course_id,
                topic=topic,
                difficulty=difficulty,
                total_delta=total_delta,
                correct_delta=correct_delta,
            )


def _extract_matrix(item):
    matrix = (item or {}).get("matrix")
    if not isinstance(matrix, dict):
        return {}
    return matrix


def _subtract_deltas_from_matrix(matrix, deltas):
    """Return updated matrix; raise MatrixDriftError if any cell would go negative."""
    working = {}
    for topic_key, difficulties in matrix.items():
        if not isinstance(difficulties, dict):
            continue
        working[topic_key] = {}
        for difficulty, cell in difficulties.items():
            if not isinstance(cell, dict):
                continue
            working[topic_key][difficulty] = {
                "correct": _safe_int(cell.get("correct")),
                "total": _safe_int(cell.get("total")),
            }

    for topic, by_difficulty in deltas.items():
        for difficulty, bucket in by_difficulty.items():
            total_delta = _safe_int(bucket.get("total"))
            correct_delta = _safe_int(bucket.get("correct"))
            if total_delta == 0 and correct_delta == 0:
                continue

            topic_cells = working.setdefault(topic, {})
            cell = topic_cells.setdefault(difficulty, {"correct": 0, "total": 0})
            new_total = cell["total"] - total_delta
            new_correct = cell["correct"] - correct_delta
            if new_total < 0 or new_correct < 0 or new_correct > new_total:
                raise MatrixDriftError(
                    f"underflow topic={topic} difficulty={difficulty} "
                    f"correct={cell['correct']}-{correct_delta} total={cell['total']}-{total_delta}"
                )
            if new_total == 0 and new_correct == 0:
                if difficulty in topic_cells:
                    del topic_cells[difficulty]
            else:
                cell["total"] = new_total
                cell["correct"] = new_correct

        if topic in working and not working[topic]:
            del working[topic]

    return working


def apply_negative_matrix_deltas(table, user_name, course_id, deltas):
    result = table.get_item(Key=_matrix_key(user_name=user_name, course_id=course_id))
    matrix = _extract_matrix(result.get("Item"))
    updated = _subtract_deltas_from_matrix(matrix, deltas)
    table.update_item(
        Key=_matrix_key(user_name=user_name, course_id=course_id),
        UpdateExpression="SET #matrix = :matrix",
        ExpressionAttributeNames=_matrix_names_root(),
        ExpressionAttributeValues={":matrix": updated},
    )


def _query_all_attempts_for_course(attempts_table, user_name, course_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "KeyConditionExpression": Key("user_name").eq(user_name),
            "FilterExpression": Attr("course_id").eq(course_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = attempts_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def _query_questions_by_set_id(questions_table, set_id, questions_set_index):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {
            "IndexName": questions_set_index,
            "KeyConditionExpression": Key("set_id").eq(set_id),
        }
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = questions_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def _answers_map_from_rows(answer_rows):
    answers = {}
    for row in answer_rows:
        question_id = row.get("question_id")
        if question_id:
            answers[question_id] = row.get("user_answer")
    return answers


def resolve_attempt_deltas(
    attempt_item,
    *,
    questions_table,
    questions_set_index,
    answer_rows,
):
    """Primary: stored matrix_deltas; fallback: rebuild from questions + answers."""
    stored = attempt_item.get("matrix_deltas")
    if stored:
        return deserialize_deltas(stored)

    set_id = attempt_item.get("question_set_id")
    if not set_id:
        raise ValueError("missing question_set_id and matrix_deltas")

    questions = _query_questions_by_set_id(questions_table, set_id, questions_set_index)
    if not questions:
        raise ValueError("questions unavailable for legacy delta reconstruction")

    answers = _answers_map_from_rows(answer_rows)
    return build_matrix_deltas_from_questions(questions, answers)


def rebuild_user_progress_matrix(
    *,
    user_progress_table,
    attempts_table,
    questions_table,
    questions_set_index,
    answers_table,
    user_name,
    course_id,
    exclude_attempt_id=None,
):
    """Replace user_progress.matrix with sum of active attempts' deltas."""
    attempts = _query_all_attempts_for_course(attempts_table, user_name, course_id)
    delta_list = []

    for attempt in attempts:
        attempt_id = attempt.get("attempt_id")
        if exclude_attempt_id and attempt_id == exclude_attempt_id:
            continue
        if not attempt.get("progress_applied_at"):
            continue
        if attempt.get("matrix_reverted_at"):
            continue

        answer_rows = []
        if attempt_id:
            answer_rows = _query_answers_for_attempt(answers_table, attempt_id)

        try:
            deltas = resolve_attempt_deltas(
                attempt,
                questions_table=questions_table,
                questions_set_index=questions_set_index,
                answer_rows=answer_rows,
            )
        except ValueError:
            logger.warning(
                "rebuild skip attempt_id=%s user=%s course=%s",
                attempt_id,
                user_name,
                course_id,
            )
            continue

        if deltas:
            delta_list.append(deltas)

    merged = merge_deltas(*delta_list)
    serialized = serialize_deltas(merged)
    user_progress_table.update_item(
        Key=_matrix_key(user_name=user_name, course_id=course_id),
        UpdateExpression="SET #matrix = :matrix",
        ExpressionAttributeNames=_matrix_names_root(),
        ExpressionAttributeValues={":matrix": serialized},
    )
    return serialized


def _query_answers_for_attempt(answers_table, attempt_id):
    items = []
    last_evaluated_key = None
    while True:
        query_args = {"KeyConditionExpression": Key("attempt_id").eq(attempt_id)}
        if last_evaluated_key:
            query_args["ExclusiveStartKey"] = last_evaluated_key
        result = answers_table.query(**query_args)
        items.extend(result.get("Items", []))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
