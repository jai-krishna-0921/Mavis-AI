"""Guards for data read from Gmail and Slack before it can be stored, logged or learned.

Two independent parts, both shared by every ingest path (webhook, poll, first sync):

1. redact(): masks secrets in free text. Detectors are measured, not templates: a code or password is
   masked because of where it sits relative to a keyword, API keys by prefix shape or by length and
   entropy, card numbers by Luhn plus network prefix, IBANs by the mod-97 checksum, Aadhaar by Verhoeff,
   PAN by its structure, Indian bank account numbers by an account keyword plus a digit run. Order
   numbers, dates, times, prices and phone numbers fail those checks and survive.
2. source filters: should_ingest_email() and should_ingest_slack() decide from structure only (labels,
   headers, subtype, bot ids and the user's mute list) whether a message may enter the knowledge graph.
"""

# ruff: noqa: E501
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from mavis.store.repo import users

MUTE_KEY = "mute"
MUTE_CAP = 200  # per list; the state row stays small


# --- redaction ---------------------------------------------------------------------------------------


def _mask(kind: str) -> str:
    return f"[redacted:{kind}]"


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _fold(s: str) -> str:
    return _strip_accents(s).casefold()


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
        alt = not alt
    return total % 10 == 0


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def iban_ok(compact: str) -> bool:
    if not 15 <= len(compact) <= 34 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    moved = compact[4:] + compact[:4]
    try:
        number = "".join(str(int(ch, 36)) for ch in moved)
    except ValueError:
        return False
    return int(number) % 97 == 1


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _card_prefix_ok(d: str) -> bool:
    """Issuer ranges and lengths of the major card networks. A random 13 to 19 digit number (an order
    or reference id) passes Luhn one time in ten; it rarely also starts like a card."""
    n = len(d)
    if d[0] == "4":
        return n in (13, 16, 19)
    if d[:2] in ("34", "37"):
        return n == 15
    if 51 <= int(d[:2]) <= 55 or 2221 <= int(d[:4]) <= 2720:
        return n == 16
    if d[:2] in ("36", "38", "39") or 300 <= int(d[:3]) <= 305:
        return n == 14
    if d[:2] == "35":
        return 16 <= n <= 19
    if d[:4] == "6011" or d[:2] in ("65", "62", "60", "81", "82") or d[:3] == "508":
        return 16 <= n <= 19
    return False


_NOT_A_CARD_CONTEXT = re.compile(
    r"(?:order|invoice|tracking|awb|consignment|shipment|ref(?:erence)?|txn|transaction|booking|ticket|"
    r"pnr|case|ticket|receipt)[\w .:#/-]{0,16}$", re.IGNORECASE)

_CARD_RUN = re.compile(r"(?<![\w.#/+])\d(?:[ -]?\d){12,22}")
_IBAN_RUN = re.compile(r"(?<![\w])[A-Z]{2}\d{2}(?:[ ]?[A-Za-z0-9]){11,30}")
_AADHAAR_RUN = re.compile(r"(?<![\w.#/+-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\w]|[.,]\d)")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL)
_JWT = re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}(?![\w-])")
# Vendor key shapes: a fixed prefix then a long random body.
_KEY_SHAPES = re.compile(
    r"(?<![\w-])(?:"
    r"sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"
    r"|xox[abprsoe]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|ya29\.[0-9A-Za-z_-]{20,}|1//[0-9A-Za-z_-]{30,}"
    r"|(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"
    r"|SG\.[0-9A-Za-z_-]{16,}\.[0-9A-Za-z_-]{16,}"
    r"|npm_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}"
    r")(?![\w-])"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{16,})")
_SECRET_PARAM_NAMES = (
    r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|secret|"
    r"token|passwd|pwd|password|private[_-]?key|session[_-]?id|sessionid)"
)
_SIGNED_PARAM = re.compile(r"(?i)((?<![A-Za-z])(?:x-amz-)?signature|sig)=([^\s\"'&<>]{8,})")
_SECRET_PARAM = re.compile(
    rf"(?i)((?<![A-Za-z]){_SECRET_PARAM_NAMES}[\"']?\s*[=:]\s*[\"']?)([^\s\"'&<>]{{8,}})")
_LONG_TOKEN = re.compile(r"(?<![\w/+=.-])[A-Za-z0-9][A-Za-z0-9_+=/-]{23,}(?![\w+=/-])")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Words that mean "a secret value follows". Vocabulary, not reply templates: the value is matched by shape.
_OTP_WORDS = frozenset({
    "otp", "code", "codes", "pin", "cvv", "cvc", "cvv2", "passcode", "token", "tan", "mfa", "2fa",
    "verification", "verify", "verifizierung", "verifizierungscode", "bestatigungscode", "sicherheitscode",
    "codigo", "codice", "kode", "kod", "securite", "seguridad", "sicherheit",
    "код", "пароль", "verificacion", "verificação", "verificacao", "doğrulama", "dogrulama",
})
_PASSWORD_WORDS = frozenset({
    "password", "passwort", "passwd", "pwd", "pass", "passphrase", "contrasena", "contrasenia", "senha",
    "motdepasse", "parola", "wachtwoord", "losenord", "salasana", "haslo", "heslo", "пароль",
})
_NOT_SECRET_PREFIX = frozenset({
    "order", "invoice", "tracking", "booking", "reference", "ref", "confirmation", "promo", "coupon",
    "discount", "zip", "postal", "area", "country", "product", "item", "ticket", "case", "gst", "hsn",
    "sku", "voucher", "gift", "referral", "airport", "station", "status", "error", "http", "response",
})
_COPULAS = frozenset({"is", "are", "was", "es", "ist", "est", "e", "sono", "ar", "equals", "="})  # accent-folded
_FILLER = frozenset({"your", "the", "a", "an", "is", "are", "was", "es", "ist", "est", "su", "tu", "votre",
                     "ihr", "ihre", "il", "la", "le", "der", "die", "das", "den", "one-time", "one", "time",
                     "temporary", "new", "temp", "login", "for", "of", "to", "use", "enter", "as", "dein",
                     "deine", "seu", "sua", "tuo", "e", "è", "son", "sono", "mi", "mon", "ton",
                     "our", "my", "this", "that", "here", "below", "following", "otp", "code", "pin",
                     "verification", "security", "sms", "email", "mobile", "number", "no", "-", ":", "=",
                     "(", ")", "\u2013", "\u2192", "->", "=>", "ist:", "is:", "mfa", "2fa", "auth", "authentication",
                     "confirmation", "access", "single-use", "single", "di", "de", "del", "du", "des", "von",
                     "da", "do", "lautet", "verifica", "verificacion", "verificação", "bestatigung", "validation", "validacion", "con", "per", "pour", "fur", "für", "zur"})

_CJK_CODE = re.compile(
    r"((?:验证码|驗證碼|校验码|動態密碼|动态密码|認証コード|認証番号|確認コード|인증번호|인증코드|확인코드|"
    r"安全码|安全碼|密码|密碼|パスワード|비밀번호)[^\d\n]{0,8})([A-Za-z0-9]{4,10})")

_TOKEN = re.compile(r"\S+")
_EDGE = ".,;:!?()[]{}<>\"'`*_“”‘’«»"
_DATE_LIKE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:am|pm)?$", re.IGNORECASE)
_MONEY_LIKE = re.compile(r"^[\$€£¥₹]|^\d{1,3}(?:,\d{2,3})+(?:\.\d+)?$|^\d+\.\d+$|%$")
_CURRENCY_AFTER = frozenset({"usd", "eur", "gbp", "inr", "rs", "rs.", "rupees", "dollars", "euro", "euros",
                             "percent", "%", "minutes", "mins", "min", "hours", "hrs", "days", "seconds",
                             "sec", "seconds.", "mg", "kg", "km"})


def _bare(token: str) -> str:
    return token.strip(_EDGE)


def _code_shaped(tok: str) -> bool:
    """A one-time code, PIN or short secret: 4 to 10 chars, digits-led, no money, date or time shape."""
    t = _bare(tok)
    if not 4 <= len(t) <= 12 or _DATE_LIKE.match(t) or _TIME_LIKE.match(t) or _MONEY_LIKE.search(t):
        return False
    if "-" in t:
        if t.count("-") > 2 or any(not (p.isalnum()) for p in t.split("-")):
            return False
    elif not t.isalnum():
        return False
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    if digits == len(t.replace("-", "")):
        return digits >= 4
    return digits >= 3 and letters >= 1 and len(t) >= 5 and digits * 2 >= letters


def _secret_shaped(tok: str) -> bool:
    t = _bare(tok)
    return len(t) >= 4 and not t.startswith(("[redacted", "http")) and "@" not in t


LOOKAHEAD = 10  # tokens after a keyword in which its value may sit ("OTP for this transaction is 482913")


def _sentence_end(raw: str) -> bool:
    b = _bare(raw)
    return raw.endswith(("!", "?")) or (raw.endswith(".") and (len(b) > 3 or any(c.isdigit() for c in b)))


def _is_copula(raw: str) -> bool:
    b = _fold(_bare(raw))
    return b in _COPULAS or raw.endswith((":", "=")) or raw in (":", "=") or "=" in raw


def _scan_secret_after(text: str, words: frozenset[str], kind: str, shaped: Callable[[str], bool],
                       *, need_copula: bool, back: bool) -> str:
    """For each keyword token, mask the value-shaped token that follows it: directly after only filler
    words, or later in the same sentence when it follows a copula or colon ("... is 482913").
    With `back`, also a code that precedes the keyword joined by a copula ("482913 is your code")."""
    toks = list(_TOKEN.finditer(text))
    spans: list[tuple[int, int]] = []

    def span(m: re.Match[str]) -> tuple[int, int]:
        raw = m.group(0)
        lead = len(raw) - len(raw.lstrip(_EDGE))
        return m.start() + lead, m.start() + lead + len(_bare(raw))

    for i, m in enumerate(toks):
        if _fold(_bare(m.group(0))).replace(" ", "") not in words:
            continue
        if i and _fold(_bare(toks[i - 1].group(0))) in _NOT_SECRET_PREFIX:
            continue
        saw_copula = _is_copula(m.group(0))
        only_filler = True
        for j in range(i + 1, min(len(toks), i + 1 + LOOKAHEAD)):
            raw = toks[j].group(0)
            if shaped(raw) and (_is_copula(toks[j - 1].group(0)) or (only_filler and (saw_copula or not need_copula))):
                spans.append(span(toks[j]))
                break
            if _is_copula(raw):
                saw_copula = True
            elif _fold(_bare(raw)) not in _FILLER and _bare(raw):
                only_filler = False
            if _sentence_end(raw):
                break
        if back and i >= 2:
            k, steps = i - 1, 0
            while k >= 0 and steps < 5:
                b = _fold(_bare(toks[k].group(0)))
                if b in _FILLER or b in _COPULAS:
                    k -= 1
                    steps += 1
                    continue
                if shaped(toks[k].group(0)) and any(_is_copula(toks[x].group(0)) for x in range(k + 1, i)):
                    spans.append(span(toks[k]))
                break
    return _apply_spans(text, spans, kind)


def _apply_spans(text: str, spans: Iterable[tuple[int, int]], kind: str) -> str:
    out, last = [], 0
    for a, b in sorted(set(spans)):
        if a < last:
            continue
        out.append(text[last:a])
        out.append(_mask(kind))
        last = b
    out.append(text[last:])
    return "".join(out)


def _sub_checked(pattern: re.Pattern[str], text: str, kind: str, ok: Callable[[re.Match[str]], bool]) -> str:
    return pattern.sub(lambda m: _mask(kind) if ok(m) else m.group(0), text)


def _mask_longest(pattern: re.Pattern[str], text: str, kind: str,
                  valid: Callable[[str, int], bool], *, unit: re.Pattern[str]) -> str:
    """Mask the longest prefix of each match that `valid` accepts. A greedy run swallows the digits or
    words that follow the real number ("... 1111 12/26"), so every end position is tried, longest first."""
    out, last = [], 0
    for m in pattern.finditer(text):
        if m.start() < last:
            continue
        ends = [e.end() for e in unit.finditer(m.group(0))]
        for end in reversed(ends):
            compact = re.sub(r"[ -]", "", m.group(0)[:end])
            after = text[m.start() + end:m.start() + end + 1]
            if (not after or not after.isalnum()) and valid(compact, m.start()):
                out.append(text[last:m.start()])
                out.append(_mask(kind))
                last = m.start() + end
                break
    out.append(text[last:])
    return "".join(out)


_DIGIT = re.compile(r"\d")
_ALNUM = re.compile(r"[A-Za-z0-9]")


def _redact_cards(text: str) -> str:
    def valid(digits: str, start: int) -> bool:
        if not (13 <= len(digits) <= 19 and luhn_ok(digits) and _card_prefix_ok(digits)):
            return False
        return not _NOT_A_CARD_CONTEXT.search(text[max(0, start - 24):start])

    return _mask_longest(_CARD_RUN, text, "card", valid, unit=_DIGIT)


def _redact_iban(text: str) -> str:
    return _mask_longest(_IBAN_RUN, text, "iban", lambda c, _s: iban_ok(c.upper()), unit=_ALNUM)


_AADHAAR_WORD = re.compile(r"(?i)aadh?a+r|uidai|\buid\b|आधार")


def _redact_aadhaar(text: str) -> str:
    """12 digits whose Verhoeff check digit holds. A random 12 digit number passes one time in ten, so
    the contiguous form also needs an Aadhaar keyword nearby; the 4-4-4 grouped form stands on its own."""
    def ok(m: re.Match[str]) -> bool:
        raw = m.group(0)
        if not verhoeff_ok(re.sub(r"[ -]", "", raw)):
            return False
        near = text[max(0, m.start() - 40):m.start()]
        if _NOT_A_CARD_CONTEXT.search(near):
            return False
        return bool(re.search(r"[ -]", raw)) or bool(_AADHAAR_WORD.search(near))

    return _sub_checked(_AADHAAR_RUN, text, "aadhaar", ok)


_ACCOUNT_WORDS = re.compile(
    r"(?i)(?<![\w])(?:a/c|a\\c|acct?\.?|account|acc\.?)(?:\s*(?:no|number|num|nbr|#)\.?)?\s*(?:is|:|=|-|#)?\s*"
    r"(?:xx+|\*+|x+)?[\s:#-]*(\d[\d ]{7,22}\d)(?![\d])")


def _redact_accounts(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\s", "", m.group(1))
        if not 9 <= len(digits) <= 18 or len(set(digits)) < 3:
            return m.group(0)
        return m.group(0).replace(m.group(1), _mask("account"))

    return _ACCOUNT_WORDS.sub(sub, text)


def _redact_secret_params(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        value = m.group(2)
        if value.startswith("[redacted") or _UUID.match(value):
            return m.group(0)
        return m.group(1) + _mask("secret")

    text = _SIGNED_PARAM.sub(lambda m: m.group(1) + "=" + _mask("secret"), text)
    return _SECRET_PARAM.sub(sub, text)


def _natural(t: str) -> bool:
    """Words and slugs, not random: file names, camel-case identifiers, hyphenated titles."""
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 12 and sum(c.lower() in "aeiou" for c in letters) / len(letters) >= 0.28 \
            and sum(c.isdigit() for c in t) <= 0.1 * len(t):
        return True
    parts = [p for p in re.split(r"[_-]", t) if p]
    if len(parts) >= 3:
        wordy = sum(p.isalpha() and len(p) >= 3 for p in parts)
        return wordy * 2 >= len(parts)
    return False


def _redact_long_tokens(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        t = m.group(0)
        if _UUID.match(t) or t.startswith("[redacted"):
            return t
        has_digit, has_alpha = any(c.isdigit() for c in t), any(c.isalpha() for c in t)
        classes = sum([any(c.islower() for c in t), any(c.isupper() for c in t), has_digit,
                       any(c in "+/=_-" for c in t)])
        # a long path segment of a URL is an id, not a credential; a token of mixed classes is
        if t.count("/") >= 2 or "/" in t and not any(c in t for c in "+="):
            return t
        if _natural(t):
            return t
        if has_digit and has_alpha and classes >= 3 and entropy(t) >= 3.5:
            return _mask("secret")
        if has_digit and has_alpha and len(t) >= 32 and entropy(t) >= 3.3 and classes >= 2:
            return _mask("secret")
        return t

    return _LONG_TOKEN.sub(sub, text)


def redact(text: str) -> str:
    """Text with credentials and financial identifiers masked as [redacted:<kind>]. Pure and idempotent."""
    if not text:
        return text
    t = text
    t = _PRIVATE_KEY.sub(_mask("private-key"), t)
    t = _JWT.sub(_mask("token"), t)
    t = _KEY_SHAPES.sub(_mask("key"), t)
    t = _BEARER.sub(lambda m: f"{m.group(1)} {_mask('token')}", t)
    t = _redact_secret_params(t)
    t = _redact_iban(t)
    t = _redact_cards(t)
    t = _redact_aadhaar(t)
    t = _PAN.sub(_mask("pan"), t)
    t = _redact_accounts(t)
    t = _scan_secret_after(t, _PASSWORD_WORDS, "password", _secret_shaped, need_copula=True, back=False)
    t = _scan_secret_after(t, _OTP_WORDS, "otp", _code_shaped, need_copula=False, back=True)
    t = _CJK_CODE.sub(lambda m: m.group(1) + _mask("otp"), t)
    t = _redact_long_tokens(t)
    return t


# --- source filters ----------------------------------------------------------------------------------

EXCLUDED_LABELS = frozenset({"SPAM", "TRASH"})
BULK_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})
SLACK_SYSTEM_SUBTYPES_KEEP = frozenset({"", "thread_broadcast", "file_share", "me_message"})
SLACK_USER_MESSAGE_SUBTYPES = SLACK_SYSTEM_SUBTYPES_KEEP


@dataclass(frozen=True)
class Mute:
    """The user's mute list, from users.state["mute"]."""

    senders: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    slack: frozenset[str] = frozenset()  # Slack channel ids (C.., G.., D..) and user ids (U.., W..)

    def hits(self, *, address: str = "", slack_ids: Iterable[str] = ()) -> str:
        """The rule that matches, or ''."""
        addr = address.strip().lower()
        if addr and addr in self.senders:
            return "muted_sender"
        domain = addr.rpartition("@")[2]
        while domain:
            if domain in self.domains:
                return "muted_domain"
            _, dot, domain = domain.partition(".")
            if not dot:
                break
        if any(i and i in self.slack for i in slack_ids):
            return "muted_slack"
        return ""

    @staticmethod
    def from_state(raw: dict | None) -> Mute:
        raw = raw or {}
        return Mute(frozenset(map(str, raw.get("senders") or ())), frozenset(map(str, raw.get("domains") or ())),
                    frozenset(map(str, raw.get("slack") or ())))


@dataclass(frozen=True)
class IngestDecision:
    """lane: "graph" (learn it), "bulk" (existing bulk lane only) or "drop" (nothing is kept)."""

    lane: str
    reason: str = ""
    flags: dict[str, Any] = field(default_factory=dict, compare=False)

    def __bool__(self) -> bool:
        return self.lane == "graph"


def should_ingest_email(n: dict, mute: Mute | None = None) -> IngestDecision:
    """Structural decision for one normalized email (normalize_email's keys)."""
    labels = {str(x).upper() for x in n.get("labels") or []}
    if labels & EXCLUDED_LABELS:
        return IngestDecision("drop", "spam_or_trash")
    if n.get("from_me") or "SENT" in labels or "DRAFT" in labels:
        return IngestDecision("drop", "own_mail")
    rule = (mute or Mute()).hits(address=str(n.get("from_address", "")))
    if rule:
        return IngestDecision("drop", rule)
    if labels & BULK_LABELS:
        return IngestDecision("bulk", "promotional_label")
    headers = {str(k).lower(): str(v) for k, v in (n.get("headers") or {}).items()}
    if n.get("list_unsubscribe") or "list-unsubscribe" in headers:
        return IngestDecision("bulk", "list_unsubscribe")
    precedence = headers.get("precedence", "").strip().lower()
    if precedence in ("bulk", "list", "junk") or headers.get("auto-submitted", "").lower() not in ("", "no"):
        return IngestDecision("bulk", "bulk_header")
    return IngestDecision("graph")


def should_ingest_slack(n: dict, mute: Mute | None = None) -> IngestDecision:
    """Structural decision for one normalized Slack message (normalize_slack's keys plus subtype/bot_id)."""
    subtype = str(n.get("subtype") or "")
    if subtype not in SLACK_USER_MESSAGE_SUBTYPES:
        return IngestDecision("drop", "system_subtype")
    if n.get("bot_id") or n.get("bot") or n.get("is_bot") or n.get("app_id") or n.get("bot_profile"):
        return IngestDecision("drop", "bot")
    if not str(n.get("user") or "").strip():
        return IngestDecision("drop", "no_author")
    if not str(n.get("text") or "").strip():
        return IngestDecision("drop", "empty")
    rule = (mute or Mute()).hits(slack_ids=(str(n.get("channel", "")), str(n.get("user", ""))))
    if rule:
        return IngestDecision("drop", rule)
    return IngestDecision("graph")


# --- the mute list in the user's settings ------------------------------------------------------------

_SLACK_ID = re.compile(r"^[CDGUW][A-Z0-9]{6,}$")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def parse_mute_target(raw: str) -> tuple[str, str] | None:
    """(list, normalized value) for what the user typed, or None. A bare name like #general cannot be
    matched to an id without Slack, so only ids are accepted for Slack."""
    t = raw.strip().strip("<>").lstrip("#@").strip()
    if not t:
        return None
    if _SLACK_ID.match(t) and any(c.isdigit() for c in t) and not _ADDRESS.match(t):
        return "slack", t.upper()
    if _ADDRESS.match(t):
        return "senders", t.lower()
    d = t.lower().lstrip("@")
    if _DOMAIN.match(d):
        return "domains", d
    return None


async def load_mute(user_id: int) -> Mute:
    return Mute.from_state((await users.get_state(user_id)).get(MUTE_KEY))


async def add_mute(user_id: int, target: str) -> tuple[str, str] | None:
    parsed = parse_mute_target(target)
    if parsed is None:
        return None
    kind, value = parsed

    def change(cur: dict) -> dict:
        items = [x for x in cur.get(kind, []) if x != value]
        return {**cur, kind: [*items, value][-MUTE_CAP:]}

    await users.modify_nested(user_id, MUTE_KEY, change)
    return parsed


async def remove_mute(user_id: int, target: str) -> tuple[str, str] | None:
    parsed = parse_mute_target(target)
    if parsed is None:
        return None
    kind, value = parsed
    await users.modify_nested(user_id, MUTE_KEY, lambda cur: {**cur, kind: [x for x in cur.get(kind, []) if x != value]})
    return parsed


def describe_mute(mute: Mute) -> str:
    parts = [*sorted(mute.senders), *sorted(mute.domains), *sorted(mute.slack)]
    if not parts:
        return "Nothing is muted. Use /mute with a sender address, a domain, or a Slack channel or user id."
    return "Muted: " + ", ".join(parts) + ". Use /unmute to undo one."
