"""R4 guardrails. Everything that comes back from a tool is untrusted data.

Defence in depth, from outermost to innermost:

  1. Field allow-list: customer_email / customer_phone are never copied into
     what the model sees (`safe_order`). The model has no use for them.
  2. PII regex redaction over ALL remaining text (`redact_pii`), because PII
     also hides in free text (see order 1004's note). Applied to tool output,
     to the user's own message, and again to anything crossing an agent boundary.
  3. Injection neutralisation (`neutralize_note`): instruction-like customer
     notes are replaced by a fixed placeholder, so the text never reaches the
     model at all. Notes that pass are fenced in <untrusted_customer_note> tags
     and the system prompts tell the model those tags contain data, not orders.
  4. Structural defence (not in this file): even a fully successful injection
     cannot change data, because refund/cancel only run after a human types an
     explicit approval, parsed deterministically outside the model.

Layer 3 is a heuristic and will miss novel phrasings; layer 4 is the one that
does not depend on detecting anything. DESIGN.md discusses this trade-off.
"""

import re
import unicodedata
from typing import Any

# ---- PII ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# International-ish phone numbers: optional +, then 7+ digits with common
# separators. Requires at least 7 digits so totals/ids/dates are not hit.
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{5,}\d(?![\w.])")
_MIN_PHONE_DIGITS = 7

REDACTED_EMAIL = "[redacted-email]"
REDACTED_PHONE = "[redacted-phone]"


_DATE_RE = re.compile(r"\s*\d{4}-\d{2}-\d{2}(\s+\d{1,2})?\s*")  # ISO dates are not phone numbers


def _phone_sub(match: re.Match) -> str:
    text = match.group(0)
    if _DATE_RE.fullmatch(text):
        return text
    digits = sum(ch.isdigit() for ch in text)
    return REDACTED_PHONE if digits >= _MIN_PHONE_DIGITS else text


def redact_pii(text: str) -> str:
    """Replace email addresses and phone numbers in free text."""
    text = EMAIL_RE.sub(REDACTED_EMAIL, text)
    return PHONE_RE.sub(_phone_sub, text)


def redact_obj(value: Any) -> Any:
    """Recursively redact every string inside dicts/lists."""
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


# ---- Prompt injection -----------------------------------------------------------
# Signals that a piece of *data* is trying to talk to an AI. Each is cheap to
# check and each alone is enough to quarantine a customer note: a real customer
# note has no reason to address "the assistant" or mention "your instructions".
_INJECTION_PATTERNS = [
    r"\b(assistant|system|developer|ai|bot|model|agent)\s*:",           # role prefixes
    r"\bignore\b.{0,40}\b(instruction|rule|prompt|polic|previous|above)",  # override
    r"\b(disregard|forget|override|bypass)\b.{0,40}\b(instruction|rule|prompt|polic|previous|above)",
    r"\byou are now\b|\bact as\b|\bpretend to be\b|\badmin mode\b|\bjailbreak\b",
    r"\b(refund|cancel|delete|approve)\b.{0,40}\b(immediately|right now|now|without)\b",
    r"\b(send|email|forward|leak|export)\b.{0,40}\b(customer|email|data|token|password)",
    r"<\s*/?\s*(system|instructions?|prompt)\s*>",                      # fake tags
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE | re.DOTALL)

NEUTRALIZED_NOTE = "[customer note withheld: it contained instruction-like text]"


def _normalize(text: str) -> str:
    # Fold look-alike characters (full-width letters, etc.) and drop zero-width /
    # control characters that are used to hide instructions from regexes.
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(_normalize(text)))


def neutralize_note(note: str) -> tuple[str, bool]:
    """Return (safe_text, flagged). Flagged notes are replaced entirely."""
    if not note:
        return "", False
    if looks_like_injection(note):
        return NEUTRALIZED_NOTE, True
    return f"<untrusted_customer_note>{redact_pii(_normalize(note))}</untrusted_customer_note>", False


# ---- Order projection -----------------------------------------------------------
SAFE_ORDER_FIELDS = ("id", "customer_name", "status", "total", "placed_at")


def safe_order(order: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    """Project a raw store order to what the model may see.

    `enabled=False` exists ONLY so a test can prove the guardrail is what stops
    the injection (a guardrail you cannot switch off cannot be shown to matter).
    """
    if not enabled:
        return dict(order)
    out = {k: order[k] for k in SAFE_ORDER_FIELDS if k in order}
    note, flagged = neutralize_note(order.get("customer_note") or "")
    out["customer_note"] = note
    if flagged:
        out["note_flagged"] = True
    return redact_obj(out)
