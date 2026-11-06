import re
import unicodedata
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{5,}\d(?![\w.])")
_MIN_PHONE_DIGITS = 7

REDACTED_EMAIL = "[redacted-email]"
REDACTED_PHONE = "[redacted-phone]"


_DATE_RE = re.compile(r"\s*\d{4}-\d{2}-\d{2}(\s+\d{1,2})?\s*")


def _phone_sub(match: re.Match) -> str:
    text = match.group(0)
    if _DATE_RE.fullmatch(text):
        return text
    digits = sum(ch.isdigit() for ch in text)
    return REDACTED_PHONE if digits >= _MIN_PHONE_DIGITS else text


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub(REDACTED_EMAIL, text)
    return PHONE_RE.sub(_phone_sub, text)


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


_INJECTION_PATTERNS = [
    r"\b(assistant|system|developer|ai|bot|model|agent)\s*:",
    r"\bignore\b.{0,40}\b(instruction|rule|prompt|polic|previous|above)",
    r"\b(disregard|forget|override|bypass)\b.{0,40}\b(instruction|rule|prompt|polic|previous|above)",
    r"\byou are now\b|\bact as\b|\bpretend to be\b|\badmin mode\b|\bjailbreak\b",
    r"\b(refund|cancel|delete|approve)\b.{0,40}\b(immediately|right now|now|without)\b",
    r"\b(send|email|forward|leak|export)\b.{0,40}\b(customer|email|data|token|password)",
    r"<\s*/?\s*(system|instructions?|prompt)\s*>",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE | re.DOTALL)

NEUTRALIZED_NOTE = "[customer note withheld: it contained instruction-like text]"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(_normalize(text)))


def neutralize_note(note: str) -> tuple[str, bool]:
    if not note:
        return "", False
    if looks_like_injection(note):
        return NEUTRALIZED_NOTE, True
    return f"<untrusted_customer_note>{redact_pii(_normalize(note))}</untrusted_customer_note>", False


SAFE_ORDER_FIELDS = ("id", "customer_name", "status", "total", "placed_at")


def safe_order(order: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        return dict(order)
    out = {k: order[k] for k in SAFE_ORDER_FIELDS if k in order}
    note, flagged = neutralize_note(order.get("customer_note") or "")
    out["customer_note"] = note
    if flagged:
        out["note_flagged"] = True
    return redact_obj(out)
