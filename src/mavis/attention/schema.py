"""Validated shapes for the attention layer. Validated enums, booleans, numbers and dates are computed
facts; free text (counterparty, action_requested) stays untrusted wherever it flows."""

from __future__ import annotations

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
_NUMBER = re.compile(r"[^\d.]")


def _enum(enum: type[StrEnum], value: Any, default: StrEnum) -> Any:
    try:
        return enum(str(value).strip().lower())
    except ValueError:
        return default


class Money(BaseModel):
    amount: float = Field(ge=0, le=1e11, description="Plain number, no currency symbol or separators")
    currency: str = Field(default="", description="ISO 4217 code such as INR or USD; empty if not stated")
    direction: Direction = Field(default=Direction.UNKNOWN, description="debit means money left the user")
    counterparty: str = Field(default="", description="Who the money went to or came from, as written")
    method: PayMethod = PayMethod.OTHER
    occurred_at: datetime | None = Field(default=None, description="ISO-8601 with offset, if stated")

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, v: Any) -> Any:
        return _NUMBER.sub("", v) or "0" if isinstance(v, str) else v

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
        if not isinstance(v, list):
            return []
        return [str(p)[:MAX_PERSON] for p in v if str(p).strip()][:MAX_PEOPLE]

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _flags(cls, v: Any) -> list[str]:
        known = {f.value for f in RiskFlag}
        out: list[str] = []
        for item in v if isinstance(v, list) else []:
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
