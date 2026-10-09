"""Envelope encryption for native grant tokens.

A fresh AES-256-GCM data key seals each value; the data key is wrapped by a KEK from NATIVE_TOKEN_KEK.
Key material is never logged and never put in an exception message.

Wrapped data keys are self-describing so the KEK can be rotated without touching ciphertext:
  b"E1" + <4-byte key id> + nonce + ct     key id is sha256(key)[:4]

NATIVE_TOKEN_KEK is the current key (wraps and unwraps). NATIVE_TOKEN_KEK_PREVIOUS (comma list) holds
retired keys that only unwrap, so a rotation is: move the old key to PREVIOUS, set the new one, run
`rewrap_text` over the sealed columns, then drop PREVIOUS.

Every sealed value carries a `SealContext` as AES-GCM associated data (purpose, user, provider, record,
column). Copying a ciphertext into another user's row, another provider's row or another column makes
decryption fail with InvalidTag. Stored text is `n1:<wrapped>.<ciphertext>`; plaintext in a sealed column
is an error, never returned.

Nonce bound: each value uses a fresh data key, so its single data-layer nonce never repeats under one key.
The KEK layer wraps every data key with a random 96-bit nonce, safe up to about 2^32 wraps (NIST SP
800-38D); rotate well before that.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mavis.config import get_settings

_ENV = b"E1"
_AAD_KEK = b"kek"
_PREFIX = "n1:"


class NotSealedError(ValueError):
    """A column that must hold a sealed value holds something else."""


@dataclass(frozen=True)
class SealContext:
    purpose: str
    user_id: int
    provider: str
    record: str
    field: str

    def aad(self) -> bytes:
        parts = (self.purpose, str(int(self.user_id)), self.provider, self.record, self.field)
        # Length-prefixed so ("a", "bc") and ("ab", "c") can never produce the same bytes.
        return b"mavis/native/v1" + b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)


def token_context(user_id: int, provider: str, field: str) -> SealContext:
    return SealContext("native-token", user_id, provider, "", field)


def _decode_key(key_b64: str) -> bytes:
    try:
        key = base64.b64decode(key_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError("a native token KEK is not valid base64") from None
    if len(key) != 32:
        raise RuntimeError("a native token KEK must be 32 bytes, base64 encoded")
    return key


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:4]


class EnvKek:
    def __init__(self, key_b64: str, previous: tuple[str, ...] = ()) -> None:
        self._current = _decode_key(key_b64)
        self._by_id = {key_id(k): AESGCM(k) for k in (self._current, *(_decode_key(p) for p in previous))}

    @property
    def current_id(self) -> bytes:
        return key_id(self._current)

    def wrap(self, data_key: bytes) -> bytes:
        nonce = os.urandom(12)
        kid = self.current_id
        return _ENV + kid + nonce + self._by_id[kid].encrypt(nonce, data_key, _AAD_KEK)

    def unwrap(self, blob: bytes) -> bytes:
        if blob[:2] != _ENV:
            raise RuntimeError("data key was not wrapped by an environment KEK")
        aes = self._by_id.get(blob[2:6])
        if aes is None:
            raise RuntimeError("no configured native KEK matches this data key (rotated out too early?)")
        return aes.decrypt(blob[6:18], blob[18:], _AAD_KEK)


@lru_cache(maxsize=4)
def _env_kek(key_b64: str, previous: tuple[str, ...]) -> EnvKek:
    return EnvKek(key_b64, previous)


def configured() -> bool:
    return bool(get_settings().native_token_kek)


def get_kek() -> EnvKek:
    s = get_settings()
    if not s.native_token_kek:
        raise RuntimeError("no native token KEK configured (NATIVE_TOKEN_KEK)")
    previous = tuple(p.strip() for p in s.native_token_kek_previous.split(",") if p.strip())
    return _env_kek(s.native_token_kek, previous)


def seal_text(text: str | None, ctx: SealContext) -> str | None:
    if text is None:
        return None
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = nonce + AESGCM(data_key).encrypt(nonce, text.encode(), ctx.aad())
    wrapped = base64.b64encode(get_kek().wrap(data_key)).decode()
    return f"{_PREFIX}{wrapped}.{base64.b64encode(ct).decode()}"


def _split(value: str) -> tuple[bytes, bytes]:
    wrapped, sep, ct = value[len(_PREFIX):].partition(".")
    if not sep:
        raise NotSealedError("malformed sealed value")
    try:
        return base64.b64decode(wrapped, validate=True), base64.b64decode(ct, validate=True)
    except (binascii.Error, ValueError):
        raise NotSealedError("malformed sealed value") from None


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def open_text(value: str | None, ctx: SealContext) -> str | None:
    """None stays None. Anything else must be sealed: plaintext in a sealed column is an error, so a DB
    writer cannot downgrade a column to readable text."""
    if value is None:
        return None
    if not is_sealed(value):
        raise NotSealedError("a sealed column holds a value that is not sealed")
    wrapped, raw = _split(value)
    data_key = get_kek().unwrap(wrapped)
    return AESGCM(data_key).decrypt(raw[:12], raw[12:], ctx.aad()).decode()


def needs_rewrap(value: str | None) -> bool:
    if value is None:
        return False
    wrapped, _ = _split(value) if is_sealed(value) else (b"", b"")
    return wrapped[:2] != _ENV or wrapped[2:6] != get_kek().current_id


def rewrap_text(value: str | None) -> str | None:
    """Move the data key to the current KEK; the ciphertext it protects is untouched."""
    if value is None:
        return None
    if not is_sealed(value):
        raise NotSealedError("a sealed column holds a value that is not sealed")
    wrapped, raw = _split(value)
    kek = get_kek()
    new = base64.b64encode(kek.wrap(kek.unwrap(wrapped))).decode()
    return f"{_PREFIX}{new}.{base64.b64encode(raw).decode()}"
