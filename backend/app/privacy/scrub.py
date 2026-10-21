import re
from typing import Optional

# regex scrubs for the few free-text fields the privacy gateway sends (account names, merchants).
# the gateway's field allowlist is the real defense; this catches PII hiding inside allowed strings.

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# (407) 555-1234 / 407-555-1234 / +1 407 555 1234; runs before DIGITS so a phone isn't half-replaced
PHONE = re.compile(r"(?:\+1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}")
# 4+ digits: card and account numbers, masks, reference ids. "Route 66" and "7-Eleven" survive
DIGITS = re.compile(r"\d{4,}")
# "Zelle payment to Jane Doe" -> "Zelle payment to [person]": after a p2p service's to/from,
# everything up to the next scrubbed token is the counterparty
P2P = re.compile(r"\b(zelle|venmo|cash app|apple cash|paypal)\b(.*?\b(?:to|from)\s+)[^\[]*[^\[\s]", re.IGNORECASE)
# "Jane Doe's Checking", "JANE & BOB'S JOINT", "José's": in the user's own account labels a
# possessive is a person, so every word (any case, any script) before the 's goes
POSSESSIVE = re.compile(r"(?:[^\W\d_]+(?:\s*&\s*|\s+))*[^\W\d_]+(?=['’][sS]\b)")


def scrub_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    s = EMAIL.sub("[email]", s)
    s = PHONE.sub("[phone]", s)
    return DIGITS.sub("[number]", s)


def scrub_merchant(s: Optional[str]) -> Optional[str]:
    s = scrub_text(s)
    return P2P.sub(r"\1\2[person]", s) if s else s


def scrub_account_name(s: Optional[str]) -> Optional[str]:
    s = scrub_text(s)
    return POSSESSIVE.sub("[person]", s) if s else s
