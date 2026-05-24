import os
import re

# Approved default; overridden by OPENAI_MODEL_NAME env
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

# Quiz worker historical limit (generate_questions unchanged)
QUIZ_MAX_SOURCE_CHARS = 12_000

# Legacy + fallback topic pair (process + read APIs)
FALLBACK_TOPICS = [{"he": "כללי", "en": "General"}]

_openai_client = None  # lazy singleton for warm invocations


def openai_config():
    api_key = os.environ.get("OPENAI_API_KEY")
    model_name = os.environ.get("OPENAI_MODEL_NAME", DEFAULT_OPENAI_MODEL)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return api_key, model_name


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        api_key, _ = openai_config()
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def clean_model_json(raw_text):
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return text


def extract_json_payload(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return text

    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fenced_match:
        text = fenced_match.group(1).strip()

    text = clean_model_json(text)
    if not text:
        return text

    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return text[start : end + 1].strip()

    return text


def truncate_source_text(text, max_chars):
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _is_valid_topic_item(item):
    if not isinstance(item, dict):
        return False
    he = item.get("he")
    en = item.get("en")
    return (
        isinstance(he, str)
        and isinstance(en, str)
        and he.strip()
        and en.strip()
    )


def ensure_document_topics(item):
    """Return a shallow copy with valid topics or FALLBACK_TOPICS."""
    copy = dict(item)
    topics = copy.get("topics")
    if isinstance(topics, list) and topics:
        valid = [t for t in topics if _is_valid_topic_item(t)]
        if valid:
            copy["topics"] = valid
            return copy
    copy["topics"] = list(FALLBACK_TOPICS)
    return copy


def dedupe_topics_by_en(topic_lists):
    seen = {}
    for topics in topic_lists:
        for item in topics:
            if not _is_valid_topic_item(item):
                continue
            key = item["en"].strip().casefold()
            if key not in seen:
                seen[key] = {"he": item["he"].strip(), "en": item["en"].strip()}
    return list(seen.values())


def allowed_en_topic_names(bilingual_topics):
    return [t["en"] for t in bilingual_topics]


def build_canonical_topic_lookup(allowed_en_names):
    lookup = {}
    for name in allowed_en_names:
        key = name.casefold()
        if key not in lookup:
            lookup[key] = name
    return lookup
