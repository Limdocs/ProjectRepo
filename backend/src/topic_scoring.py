DIFFICULTY_WEIGHTS = {"Easy": 1, "Medium": 2, "Hard": 3}

TOPIC_STATUS_THRESHOLDS = {
    "weak_max": 59,
    "medium_max": 79,
}

_BREAKDOWN_KEYS = {"Easy": "easy", "Medium": "medium", "Hard": "hard"}
_LEGACY_TOPIC_ALIASES = {"General": "Uncategorized"}


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_topic_key(topic):
    return _LEGACY_TOPIC_ALIASES.get(topic, topic)


def _difficulty_weight(difficulty):
    return DIFFICULTY_WEIGHTS.get(difficulty, 2)


def _topic_status(score):
    if score <= TOPIC_STATUS_THRESHOLDS["weak_max"]:
        return "weak"
    if score <= TOPIC_STATUS_THRESHOLDS["medium_max"]:
        return "medium"
    return "strong"


def _merge_matrix(matrix):
    merged = {}
    if not isinstance(matrix, dict):
        return merged

    for topic_key, difficulties in matrix.items():
        if not isinstance(difficulties, dict):
            continue
        canonical = _normalize_topic_key(topic_key)
        topic_cells = merged.setdefault(canonical, {})
        for difficulty, cell in difficulties.items():
            if not isinstance(cell, dict):
                continue
            bucket = topic_cells.setdefault(difficulty, {"correct": 0, "total": 0})
            bucket["correct"] += _safe_int(cell.get("correct"))
            bucket["total"] += _safe_int(cell.get("total"))
    return merged


def compute_topic_scores(matrix):
    merged = _merge_matrix(matrix)
    topics = []

    for topic, difficulties in merged.items():
        weighted_correct = 0
        weighted_total = 0
        correct_count = 0
        total_answered = 0
        difficulty_breakdown = {
            key: {"correct": 0, "total": 0, "score": None}
            for key in ("easy", "medium", "hard")
        }

        for difficulty, cell in difficulties.items():
            if not isinstance(cell, dict):
                continue
            correct_d = _safe_int(cell.get("correct"))
            total_d = _safe_int(cell.get("total"))
            weight = _difficulty_weight(difficulty)
            weighted_correct += weight * correct_d
            weighted_total += weight * total_d
            correct_count += correct_d
            total_answered += total_d

            breakdown_key = _BREAKDOWN_KEYS.get(difficulty)
            if breakdown_key is not None:
                difficulty_breakdown[breakdown_key]["correct"] += correct_d
                difficulty_breakdown[breakdown_key]["total"] += total_d

        for breakdown in difficulty_breakdown.values():
            total_d = breakdown["total"]
            if total_d > 0:
                breakdown["score"] = round((breakdown["correct"] / total_d) * 100)

        if weighted_total == 0:
            continue

        score = round((weighted_correct / weighted_total) * 100)
        topics.append(
            {
                "topic": topic,
                "score": score,
                "status": _topic_status(score),
                "total_answered": total_answered,
                "correct_count": correct_count,
                "wrong_count": total_answered - correct_count,
                "weighted_correct": weighted_correct,
                "weighted_total": weighted_total,
                "difficulty_breakdown": difficulty_breakdown,
            }
        )

    topics.sort(key=lambda item: (item["score"], item["topic"]))
    return topics
