import pytest
from cryptography.exceptions import InvalidTag

from mavis.config import get_settings
from mavis.tools.integrations.native import crypto
from mavis.tools.integrations.native.crypto import NotSealedError, token_context

from .conftest import new_kek


def ctx(user=1, provider="google", field="access_token"):
    return token_context(user, provider, field)


def test_roundtrip_and_ciphertext_hides_plaintext(native_env):
    sealed = crypto.seal_text("ya29.secret-token", ctx())
    assert "secret-token" not in sealed and sealed.startswith("n1:")
    assert crypto.open_text(sealed, ctx()) == "ya29.secret-token"
    assert crypto.seal_text(None, ctx()) is None and crypto.open_text(None, ctx()) is None


def test_every_seal_is_distinct(native_env):
    assert crypto.seal_text("x", ctx()) != crypto.seal_text("x", ctx())


@pytest.mark.parametrize("other", [
    dict(user=2), dict(provider="slack"), dict(field="refresh_token"),
])
def test_bound_to_user_provider_and_column(native_env, other):
    sealed = crypto.seal_text("tok", ctx())
    with pytest.raises(InvalidTag):
        crypto.open_text(sealed, ctx(**other))


def test_aad_parts_cannot_be_shifted(native_env):
    a = crypto.SealContext("p", 1, "ab", "c", "f").aad()
    b = crypto.SealContext("p", 1, "a", "bc", "f").aad()
    assert a != b


@pytest.mark.parametrize("stored", ["plain-token", "v2:abc.def", "n1:nodot", "n1:!!!.???"])
def test_plaintext_or_malformed_in_sealed_column_is_rejected(native_env, stored):
    with pytest.raises(NotSealedError):
        crypto.open_text(stored, ctx())


def test_wrong_kek_cannot_open(native_env, monkeypatch):
    sealed = crypto.seal_text("tok", ctx())
    monkeypatch.setenv("NATIVE_TOKEN_KEK", new_kek())
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="no configured native KEK"):
        crypto.open_text(sealed, ctx())


def test_rotation_with_previous_key_and_rewrap(native_env, monkeypatch):
    old = native_env.native_token_kek
    sealed = crypto.seal_text("tok", ctx())
    monkeypatch.setenv("NATIVE_TOKEN_KEK", new_kek())
    monkeypatch.setenv("NATIVE_TOKEN_KEK_PREVIOUS", old)
    get_settings.cache_clear()
    assert crypto.open_text(sealed, ctx()) == "tok"
    assert crypto.needs_rewrap(sealed)
    moved = crypto.rewrap_text(sealed)
    assert not crypto.needs_rewrap(moved) and crypto.open_text(moved, ctx()) == "tok"
    monkeypatch.delenv("NATIVE_TOKEN_KEK_PREVIOUS")
    get_settings.cache_clear()
    assert crypto.open_text(moved, ctx()) == "tok"  # the old key is no longer needed


@pytest.mark.parametrize("bad", ["", "not base64!!", "c2hvcnQ="])
def test_bad_kek_is_refused_without_echoing_it(settings, monkeypatch, bad):
    monkeypatch.setenv("NATIVE_TOKEN_KEK", bad or "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        crypto.seal_text("x", ctx())
    assert bad not in str(exc.value) or bad == ""
