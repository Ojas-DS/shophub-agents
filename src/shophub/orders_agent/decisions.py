"""Turn the human's free-text reply to a proposal into approve / reject / unclear.

Deliberately NOT done by the LLM. Approval is a security decision: it must come
only from the human's own words, and must not be something tool output (e.g. an
injected customer note) could ever talk a model into. A small deterministic
parser cannot be prompt-injected, and its behaviour is fully unit-testable.

Anything that is not an unambiguous approval is NOT an approval (fail safe).
"""

import re
from typing import Literal

Decision = Literal["approve", "reject", "unclear"]

_NEGATION = re.compile(r"\b(no|not|don'?t|do not|never|stop|abort|reject\w*|decline\w*|deny|denied|cancel that|hold off|wait)\b", re.I)
_APPROVE = re.compile(r"\b(approve[ds]?|yes|yep|yeah|confirm(ed)?|go ahead|proceed|do it|ok(ay)?|sure|lgtm)\b", re.I)
_REJECT = re.compile(r"\b(reject\w*|no|nope|decline\w*|deny|denied|don'?t|do not|abort|stop|cancel that|never ?mind)\b", re.I)


def parse_decision(reply: str) -> Decision:
    text = (reply or "").strip()
    if not text:
        return "unclear"
    if _REJECT.search(text):
        return "reject"          # any refusal signal wins over an approval word
    if _APPROVE.search(text) and not _NEGATION.search(text):
        return "approve"
    return "unclear"
