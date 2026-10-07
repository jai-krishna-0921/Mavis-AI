"""One argument rule for every model-facing tool (hotfix4 H2).

A model fills tool arguments loosely: a guest list of `[""]`, a title of spaces, a recipient written as
`Name <addr>`. Left as they are, those values decide the risk class, the approval preview and what the
provider receives, so a self-only calendar block became an OUTWARD invite card with an empty "With:" line
and then a provider 400. `ToolArgs` normalises every args model before any of that is computed:

- list-of-string items (addresses, names, ids) are stripped and blank items dropped;
- a blank string (empty or only whitespace) in a REQUIRED field is an error the model sees as the tool
  result in the same turn (it can retry or ask the user), so it never reaches an approval card;
- a blank string in an optional field follows the field's meaning. A patch field (default None: "None
  keeps it") that may be empty keeps "" as "clear it" (an event's description, a task's notes); a field
  that may not be empty (min_length, as every title or name that identifies a thing declares), or has a
  real default, treats blank as not given (the default applies);
- a list whose items were all blank counts as not given; an explicit `[]` is kept (it can mean "remove
  every guest");
- fields typed `Email` must hold one email address (`Name <addr>` and `mailto:addr` become `addr`).

Non-blank text is kept exactly as written (a body's paragraph breaks, Markdown indentation). Internal
argument models that code builds (exact provider payloads) stay plain BaseModels and are not touched.
"""

from __future__ import annotations

import re
import types
import typing
from email.utils import parseaddr
from typing import Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, model_validator
from pydantic.fields import FieldInfo

_EMAIL_RE = re.compile(r"^[^@\s<>,;:\"]+@[^@\s<>,;:\"]+\.[^@\s<>,;:\"]+$")


def email_address(value: str) -> str:
    """One email address, bare. Raises ValueError (a tool error the model reads) otherwise."""
    text = value.strip()
    candidate = parseaddr(text)[1] if "<" in text else text
    if candidate[:7].lower() == "mailto:":
        candidate = candidate[7:]
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


def _clearable(field: FieldInfo) -> bool:
    """A patch field (default None) whose value may be the empty string."""
    if field.default is not None or field.default_factory is not None:
        return False
    return not any(getattr(m, "min_length", 0) for m in field.metadata)


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
            blank = not value.strip()
            if blank and not field.is_required() and _clearable(field):
                out[key] = ""  # a patch field: "" clears it, which is not the same as leaving it out
                continue
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
