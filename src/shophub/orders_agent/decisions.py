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
        return "reject"
    if _APPROVE.search(text) and not _NEGATION.search(text):
        return "approve"
    return "unclear"
