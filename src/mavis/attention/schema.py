"""Validated shapes for the attention layer. Validated enums, booleans, numbers and dates are computed
facts; free text (counterparty, action_requested) stays untrusted wherever it flows."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_ACTION = 160
MAX_COUNTERPARTY = 120
MAX_PEOPLE = 5
MAX_PERSON = 60


class EmailKind(StrEnum):
    MONEY_MOVEMENT = "money_movement"
    SECURITY = "security"
    DEADLINE_OR_BILL = "deadline_or_bill"
    REQUEST_FROM_PERSON = "request_from_person"
    TRAVEL = "travel"
    RECEIPT_OR_ORDER = "receipt_or_order"
    ACCOUNT_UPDATE = "account_update"
    NEWSLETTER = "newsletter"
    OTHER = "other"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    UNKNOWN = "unknown"


class PayMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"
    OTHER = "other"


class UrgencyHint(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class RiskFlag(StrEnum):
    NEW_SIGNIN = "new_signin"
    CREDENTIAL_CHANGE = "credential_change"
    MFA_CHANGE = "mfa_change"
    ACCOUNT_LOCKED = "account_locked"
    PAYMENT_FAILED = "payment_failed"
    OTP_CODE = "otp_code"
    ASKS_FOR_CREDENTIALS = "asks_for_credentials"
    ASKS_FOR_PAYMENT = "asks_for_payment"
    PRESSURE_LANGUAGE = "pressure_language"


class Verdict(StrEnum):
    PENDING = "pending"
    DROPPED = "dropped"  # promotional / social / spam label: logged without an LLM call
    FORWARDED = "forwarded"  # matched a watched loop: handed to the initiative reasoner
    LOG = "log"
    BRIEF = "brief"
    NOTIFY = "notify"
    ASK = "ask"


class Feedback(StrEnum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    MUTE = "mute"
    ALWAYS = "always"


LADDER: tuple[Verdict, ...] = (Verdict.LOG, Verdict.BRIEF, Verdict.NOTIFY)  # preferences move along this only
FLAG_LABELS: dict[str, str] = {
    RiskFlag.NEW_SIGNIN: "a new sign-in",
    RiskFlag.CREDENTIAL_CHANGE: "a password or recovery change",
    RiskFlag.MFA_CHANGE: "a change to two-step verification",
    RiskFlag.ACCOUNT_LOCKED: "a locked account",
    RiskFlag.PAYMENT_FAILED: "a failed payment",
    RiskFlag.OTP_CODE: "a one-time code",
    RiskFlag.ASKS_FOR_CREDENTIALS: "a request for login details",
    RiskFlag.ASKS_FOR_PAYMENT: "a request for payment",
    RiskFlag.PRESSURE_LANGUAGE: "pressure to act fast",
}
METHOD_LABELS: dict[str, str] = {
    PayMethod.CARD: "card",
    PayMethod.UPI: "UPI",
    PayMethod.BANK_TRANSFER: "bank transfer",
    PayMethod.WALLET: "wallet",
    PayMethod.OTHER: "payment",
}
CURRENCY_ALIASES = {
    "₹": "INR",
    "RS": "INR",
    "RS.": "INR",
    "RUPEE": "INR",
    "RUPEES": "INR",
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
}
_ISO = re.compile(r"^[A-Z]{3}$")
_PLAIN_NUMBER = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?$")
_AMOUNT = re.compile(r"^(?P<num>[\d.,]+)\s*(?P<unit>[a-z]*)$")
_CURRENCY_MARK = re.compile(r"^(?:₹|rs\.?|inr|usd|us\$|\$|eur|€|gbp|£)\s*|\s*(?:/-|only)$")
_UNITS = {
    "k": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "million": 1e6,
}
_SPLIT_LIST = re.compile(r"[,;\s]+")


def parse_amount(value: Any) -> float | None:
    """A plain positive amount, or None. Never guesses: sign, exponent, odd grouping, unknown words reject."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    for _ in range(2):
        text = _CURRENCY_MARK.sub("", text).strip()
    match = _AMOUNT.match(text) if text else None
    if not match and text:
        match = re.match(r"^(?P<num>[\d.,]+)\s+(?P<unit>[a-z]+)$", text)
    if not match or not _PLAIN_NUMBER.match(match["num"]):
        return None
    unit = match["unit"]
    if unit and unit not in _UNITS:
        return None
    number = float(match["num"].replace(",", "")) * _UNITS.get(unit, 1)
    return number if math.isfinite(number) and 0 < number <= 1e11 else None


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_when(value: Any, *, date_only_end: bool = False) -> datetime | None:
    """ISO-8601 from the model. The result may be naive: a value without an offset is wall-clock time in
    the user's zone and is localized by the pipeline (localize_understanding), never read as UTC. A bare
    date is the end of that day when `date_only_end` (a due date), else unknown (None)."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _DATE_ONLY.match(text):
        if not date_only_end:
            return None
        try:
            return datetime.fromisoformat(text).replace(hour=23, minute=59, second=59)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _enum(enum: type[StrEnum], value: Any, default: StrEnum) -> Any:
    try:
        return enum(str(value).strip().lower())
    except ValueError:
        return default


class Money(BaseModel):
    amount: float = Field(gt=0, le=1e11, description="Plain number, no currency symbol or separators")
    currency: str = Field(default="", description="ISO 4217 code such as INR or USD; empty if not stated")
    direction: Direction = Field(default=Direction.UNKNOWN, description="debit means money left the user")
    counterparty: str = Field(default="", description="Who the money went to or came from, as written")
    method: PayMethod = PayMethod.OTHER
    occurred_at: datetime | None = Field(default=None, description="ISO-8601 with offset, if stated")

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, v: Any) -> Any:
        parsed = parse_amount(v)
        if parsed is None:
            raise ValueError("unparseable amount")
        return parsed

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred(cls, v: Any) -> datetime | None:
        return parse_when(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any) -> str:
        code = str(v or "").strip().upper()
        code = CURRENCY_ALIASES.get(code, code)
        return code if _ISO.match(code) else ""

    @field_validator("direction", mode="before")
    @classmethod
    def _direction(cls, v: Any) -> Any:
        return _enum(Direction, v, Direction.UNKNOWN)

    @field_validator("method", mode="before")
    @classmethod
    def _method(cls, v: Any) -> Any:
        return _enum(PayMethod, v, PayMethod.OTHER)

    @field_validator("counterparty", mode="before")
    @classmethod
    def _counterparty(cls, v: Any) -> str:
        return str(v or "")[:MAX_COUNTERPARTY]


class EmailUnderstanding(BaseModel):
    kind: EmailKind = Field(description="The single best category")
    needs_user: bool = Field(default=False, description="True only if the user must do or decide something")
    action_requested: str = Field(
        default="", description="At most 12 words in your own words; no links, numbers to call or addresses"
    )
    urgency_hint: UrgencyHint = UrgencyHint.NORMAL
    money: Money | None = Field(default=None, description="Fill whenever money moved or was charged")
    deadline: datetime | None = Field(
        default=None, description="ISO-8601 with offset if a due date is stated"
    )
    people: list[str] = Field(default_factory=list, description="Names of real people involved, at most 5")
    risk_flags: list[RiskFlag] = Field(default_factory=list, description="Only flags that apply")

    @field_validator("money", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Money | None:
        if isinstance(v, Money):
            return v
        if not isinstance(v, dict):
            return None
        try:
            return Money.model_validate(v)
        except ValueError:
            return None

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, v: Any) -> datetime | None:
        return parse_when(v, date_only_end=True)

    @field_validator("needs_user", mode="before")
    @classmethod
    def _needs_user(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"true", "yes", "1"}
        return bool(v)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> Any:
        return _enum(EmailKind, v, EmailKind.OTHER)

    @field_validator("urgency_hint", mode="before")
    @classmethod
    def _urgency(cls, v: Any) -> Any:
        return _enum(UrgencyHint, v, UrgencyHint.NORMAL)

    @field_validator("action_requested", mode="before")
    @classmethod
    def _action(cls, v: Any) -> str:
        return str(v or "")[:MAX_ACTION]

    @field_validator("people", mode="before")
    @classmethod
    def _people(cls, v: Any) -> list[str]:
        items = [v] if isinstance(v, str) else v
        if not isinstance(items, list):
            return []
        return [p.strip()[:MAX_PERSON] for p in items if isinstance(p, str) and p.strip()][:MAX_PEOPLE]

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _flags(cls, v: Any) -> list[str]:
        known = {f.value for f in RiskFlag}
        out: list[str] = []
        items = _SPLIT_LIST.split(v) if isinstance(v, str) else v
        for item in items if isinstance(items, list) else []:
            value = str(item).strip().lower()
            if value in known and value not in out:
                out.append(value)
        return out


@dataclass(frozen=True)
class AnomalyResult:
    score: float = 0.0
    codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


NO_ANOMALY = AnomalyResult()


@dataclass(frozen=True)
class AttentionDecision:
    verdict: Verdict
    urgency: int
    attention: float
    reasons: tuple[str, ...] = ()
