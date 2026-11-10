"""SPEC §3.3 deny-list.

Mongolian terms match at a word start so inflected forms are caught ("кодыг", "нууц үгээ",
"нэвтрэхэд"). Latin terms match as whole words (letters only on either side), so a masked name
such as "PINO***" does not trigger while "PIN 1234" and "OTP123456" do.
"""

from __future__ import annotations

import re
import unicodedata

_MONGOLIAN = ("нэг удаагийн", "код", "нууц үг", "баталгаажуулах", "нэвтрэх")
_LATIN = ("otp", "password", "verification", "pin", "login")

_PATTERN = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in _MONGOLIAN)
    + r")|(?<![a-z])(?:"
    + "|".join(_LATIN)
    + r")s?(?![a-z])"
)


def contains_denied(text: str) -> bool:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PATTERN.search(folded) is not None
