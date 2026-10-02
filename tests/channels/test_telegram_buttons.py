import pytest

from mavis.channels.telegram import to_inline_button
from mavis.domain.messages import Button


def test_url_button_renders_as_url():
    b = to_inline_button(Button(label="Connect Gmail", url="https://accounts.google.com/x"))
    assert b.url == "https://accounts.google.com/x"
    assert b.callback_data is None


def test_callback_button_unchanged():
    b = to_inline_button(Button(label="Not now", data="conn:no:3"))
    assert b.callback_data == "conn:no:3"
    assert b.url is None


def test_button_with_neither_data_nor_url_is_rejected():
    with pytest.raises(ValueError):
        to_inline_button(Button(label="Broken"))
