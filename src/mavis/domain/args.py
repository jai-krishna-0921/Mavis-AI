"""One argument rule for every model-facing tool (hotfix4 H2).

A model fills tool arguments loosely: a guest list of `[""]`, a title of spaces, a recipient written as
`Name <addr>`. Left as they are, those values decide the risk class, the approval preview and what the
provider receives, so a self-only calendar block became an OUTWARD invite card with an empty "With:" line
and then a provider 400. `ToolArgs` normalises every args model before any of that is computed:

- every string is stripped of surrounding whitespace;
- list-of-string items are stripped and blank items dropped;
- a blank value (an empty string, or a list whose items were all blank) counts as NOT GIVEN: an optional
  field falls back to its default, a required field is an error the model sees as the tool result in the
  same turn (it can retry or ask the user), so it never reaches an approval card;
- fields typed `Email` must hold one email address (`Name <addr>` is reduced to `addr`).

An explicitly empty list (`[]`) is kept: it can mean something ("remove every guest"). Internal argument
models that code builds (exact provider payloads) stay plain BaseModels and are not touched.
"""

from __future__ import annotations

import re
import types
import typing
from email.utils import parseaddr
from typing import Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, model_validator

_EMAIL_RE = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")


def email_address(value: str) -> str:
    """One email address, bare. Raises ValueError (a tool error the model reads) otherwise."""
    text = value.strip()
    candidate = parseaddr(text)[1] if "<" in text else text
    if not _EMAIL_RE.match(candidate):
        raise ValueError(
            f"{value!r} is not an email address. Use a real address (look the person up, or ask the "
            "user); never guess one or leave it blank."
        )
    return candidate


Email = Annotated[str, AfterValidator(email_address)]

Shape = Literal["str", "strlist"]


def _unwrap(annotation: Any) -> Any:
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        rest = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(rest) == 1:
            return _unwrap(rest[0])
    return annotation


def _shape(annotation: Any) -> Shape | None:
    inner = _unwrap(annotation)
    if inner is str:
        return "str"
    if typing.get_origin(inner) is list:
        (item,) = typing.get_args(inner) or (None,)
        if _unwrap(item) is str:
            return "strlist"
    return None  # numbers, dates, enums, literals, unions such as sheet cells: left to pydantic


def normalise_args(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """The rule above, on raw input for `model`. Returns a new dict; raises ValueError on a blank
    required field."""
    out = dict(data)
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key not in out:
            continue
        shape = _shape(field.annotation)
        value = out[key]
        blank = False
        if shape == "str" and isinstance(value, str):
            value = value.strip()
            blank = not value
        elif shape == "strlist" and isinstance(value, list):
            items = [v.strip() if isinstance(v, str) else v for v in value]
            kept = [v for v in items if not (isinstance(v, str) and not v)]
            blank = bool(value) and not kept
            value = kept
        else:
            continue
        if not blank:
            out[key] = value
        elif field.is_required():
            raise ValueError(
                f"{key} is empty. Give it a real value, or ask the user for it; do not send it blank."
            )
        else:
            del out[key]  # not given: the field's default applies
    return out


class ToolArgs(BaseModel):
    """Base for every args model a model fills in (tools offered to an LLM)."""

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        return normalise_args(cls, data) if isinstance(data, dict) else data
