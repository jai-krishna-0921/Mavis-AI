"""Google sign-in: OpenID Connect authorization code flow with PKCE, scopes `openid email profile` only.

It never creates a Mavis user. The ID token is verified as Google documents: signature against the keys at
the JWKS URI (cached by Cache-Control), `iss`, `aud`, `exp`, plus our `nonce` and `email_verified`. The
redirect URI is <PUBLIC_BASE_URL>/api/v1/auth/google/callback and must be registered on the Google OAuth
client (the same client the native Google connector uses)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from mavis.config import get_settings
from mavis.tools.integrations.native.http import TOKEN_BYTES, ResponseTooLarge, make_client, send_capped
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL
from mavis.web import signed

log = structlog.get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
SCOPE = "openid email profile"
STATE_TTL_S = 600
LEEWAY_S = 60
DEFAULT_JWKS_TTL_S = 3600

STATE_COOKIE = "mavis_gsi"


class SigninError(Exception):
    """Stable `kind`; never carries vendor text."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/auth/google/callback"


def configured() -> bool:
    s = get_settings()
    return bool(s.google_signin_enabled and s.google_oauth_client_id and s.google_oauth_client_secret)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_client: httpx.AsyncClient | None = None
_jwks: dict = {"keys": {}, "expires": 0.0}


def set_client(client: httpx.AsyncClient | None) -> None:
    """Tests install a client with a mock transport. Also drops the key cache."""
    global _client
    _client = client
    _jwks.update(keys={}, expires=0.0)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = make_client()
    return _client


@dataclass(frozen=True)
class Begin:
    url: str
    cookie: str  # the signed state cookie value


def begin(*, mode: str = "signin", user_id: int | None = None) -> Begin:
    """The Google URL to send the browser to and the signed cookie that binds the answer to this browser."""
    if not configured():
        raise SigninError("not_configured")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    cookie = signed.sign("gsi", {"s": state, "n": nonce, "v": verifier, "m": mode, "u": user_id}, STATE_TTL_S)
    q = {"response_type": "code", "client_id": get_settings().google_oauth_client_id,
         "redirect_uri": redirect_uri(), "scope": SCOPE, "state": state, "nonce": nonce,
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
         "prompt": "select_account"}
    return Begin(f"{AUTH_URL}?{urlencode(q)}", cookie)


@dataclass(frozen=True)
class Verified:
    email: str
    sub: str
    mode: str
    user_id: int | None


async def finish(cookie: str | None, state: str, code: str) -> Verified:
    """Check the state against the browser's cookie, exchange the code and verify the ID token."""
    body = signed.verify("gsi", cookie)
    if body is None or not secrets.compare_digest(str(body.get("s", "")).encode(), state.encode()):
        raise SigninError("bad_state")
    s = get_settings()
    tokens = await _post(GOOGLE_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": s.google_oauth_client_id, "client_secret": s.google_oauth_client_secret,
        "code_verifier": body["v"]})
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise SigninError("exchange_failed")
    claims = await verify_id_token(id_token, nonce=str(body["n"]))
    return Verified(email=str(claims["email"]).lower(), sub=str(claims["sub"]),
                    mode=str(body.get("m", "signin")), user_id=body.get("u"))


async def _post(url: str, **kw) -> dict:
    try:
        r = await send_capped(_http(), "POST", url, max_bytes=TOKEN_BYTES, **kw)
        data = r.json()
    except (ResponseTooLarge, httpx.HTTPError, ValueError):
        raise SigninError("exchange_failed") from None
    if not 200 <= r.status_code < 300 or not isinstance(data, dict):
        raise SigninError("exchange_failed")
    return data


async def _keys(force: bool = False) -> dict[str, dict]:
    if not force and _jwks["keys"] and _jwks["expires"] > time.time():
        return _jwks["keys"]
    try:
        r = await send_capped(_http(), "GET", JWKS_URL, max_bytes=TOKEN_BYTES)
        data = r.json()
        keys = {k["kid"]: k for k in data["keys"] if k.get("kty") == "RSA" and k.get("kid")}
    except (ResponseTooLarge, httpx.HTTPError, ValueError, KeyError, TypeError):
        raise SigninError("exchange_failed") from None
    m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
    ttl = int(m.group(1)) if m else DEFAULT_JWKS_TTL_S
    _jwks.update(keys=keys, expires=time.time() + min(ttl, 86_400))
    return keys


def _segments(token: str) -> tuple[dict, dict, bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise SigninError("bad_token")
    try:
        header, claims = json.loads(_unb64(parts[0])), json.loads(_unb64(parts[1]))
        sig = _unb64(parts[2])
    except (ValueError, TypeError):
        raise SigninError("bad_token") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise SigninError("bad_token")
    return header, claims, sig, f"{parts[0]}.{parts[1]}".encode()


async def verify_id_token(token: str, *, nonce: str) -> dict:
    header, claims, sig, signing_input = _segments(token)
    if header.get("alg") != "RS256":
        raise SigninError("bad_token")
    kid = str(header.get("kid", ""))
    keys = await _keys()
    if kid not in keys:  # Google rotates keys: one forced refresh before refusing
        keys = await _keys(force=True)
    jwk = keys.get(kid)
    if jwk is None:
        raise SigninError("bad_token")
    try:
        pub = RSAPublicNumbers(int.from_bytes(_unb64(jwk["e"]), "big"),
                               int.from_bytes(_unb64(jwk["n"]), "big")).public_key()
        pub.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, KeyError, ValueError):
        raise SigninError("bad_token") from None
    now = time.time()
    try:
        exp, iat = float(claims.get("exp", 0)), float(claims.get("iat", 0))
    except (TypeError, ValueError):
        raise SigninError("bad_token") from None
    aud = claims.get("aud")
    aud_ok = aud == get_settings().google_oauth_client_id or (
        isinstance(aud, list) and get_settings().google_oauth_client_id in aud)
    if (claims.get("iss") not in ISSUERS or not aud_ok or exp < now - LEEWAY_S or iat > now + LEEWAY_S
            or not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode())):
        raise SigninError("bad_token")
    email = claims.get("email")
    if claims.get("email_verified") not in (True, "true") or not isinstance(email, str) or "@" not in email \
            or not claims.get("sub"):
        raise SigninError("email_unverified")
    return claims
