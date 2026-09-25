"""
Type-aware typing of one value into one page control.

Shared by the assisted-apply driver and the autofill executor so a checkbox, a
radio group, a <select>, a file input and a text box are each driven the way
their control actually works. Before this module existed both paths called
``locator.fill()`` for everything: ``fill()`` cannot tick a checkbox or pick a
radio, so an answered checkbox either errored out or — worse — reached the
user as an empty text box to type into.

The contract, in the spirit of the rest of the apply stack:

* the *value* never leaves this module — callers pass it, the page gets it,
  and only the control name and the action performed are ever logged;
* ``checkbox`` answers are booleans in words (``true``/``false``/``yes``/``no``
  /``on``/``off``/``1``/``0``); anything not falsy ticks the box, anything
  falsy unticks it — an explicit ``false`` is an answer ("no"), not an absence;
* ``radio`` answers are the *option* the human picked (its visible label or its
  ``value`` attribute). The group is addressed by name/id; the matching option
  is checked and no other;
* a refused or impossible fill raises — the caller records the failure. This
  module never silently "succeeds" without typing.

This is a pure executor: the decision of *what* may be typed is the field
classifier's (:mod:`app.services.field_classifier`) and the driver re-proves
every instruction against it before calling in here.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

#: Words that answer a checkbox "yes".
CHECKBOX_TRUE = ("true", "yes", "y", "1", "on", "checked", "tick", "ticked")

#: Words that answer a checkbox "no" — an explicit answer, so the box is
#: *unticked*, not left alone: the user said no to a box the page may have
#: pre-ticked.
CHECKBOX_FALSE = ("false", "no", "n", "0", "off", "unchecked", "unticked", "")

#: Input types that ``fill()`` can never drive and that need their own path.
BOOLEAN_INPUT_TYPES = ("checkbox", "radio")


def checkbox_checked(value: Any) -> bool:
    """Interpret an answer as a checkbox state. Non-falsy words tick the box."""
    text = ("" if value is None else str(value)).strip().lower()
    return text not in CHECKBOX_FALSE


def _attr(value: str) -> str:
    """Escape a value for a double-quoted CSS attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


async def _matches_label(locator, want: str) -> bool:
    """True when this control's visible label (or value) is the picked option."""
    text = (await locator.evaluate(
        "el => (el.labels && el.labels.length ? el.labels[0].innerText "
        ": (el.getAttribute('aria-label') || el.value || ''))"
    ) or "").strip()
    if not text:
        return False
    want_cf = want.strip().casefold()
    text_cf = text.casefold()
    return text_cf == want_cf or want_cf in text_cf or text_cf in want_cf


async def apply_field_value(
    page,
    selector: str,
    *,
    field_type: str,
    value: Any,
    options: Optional[Sequence[str]] = None,
    timeout: int = 5000,
) -> str:
    """Type *value* into the control at *selector*, for the control it is.

    Returns the action performed (``"fill"``, ``"checkbox"``, ``"radio"``,
    ``"select"``, ``"file"``) for the caller's log/diagnostics. Raises when
    nothing could be typed — never reports a fill that did not happen.

    ``page`` is a Playwright page (or anything exposing ``.locator``); the
    function is deliberately duck-typed so tests can drive it with fakes and
    no Playwright install.
    """
    ftype = (field_type or "text").strip().lower()
    text = "" if value is None else str(value).strip()

    if ftype == "file":
        locator = page.locator(selector).first
        if await locator.count() == 0:
            raise LookupError(f"selector {selector} matched nothing")
        await locator.set_input_files(text)
        return "file"

    if ftype == "checkbox":
        locator = page.locator(selector).first
        if await locator.count() == 0:
            raise LookupError(f"selector {selector} matched nothing")
        if checkbox_checked(value):
            await locator.check()
        else:
            await locator.uncheck()
        return "checkbox"

    if ftype == "radio":
        # The value is the option the human picked, not free text: find the one
        # radio of the group that carries it (value attribute first, then its
        # visible label) and check exactly that one.
        if not text:
            raise LookupError("radio fill needs the picked option as its value")
        chosen = page.locator(f'{selector}[value="{_attr(text)}"]').first
        if await chosen.count() == 0:
            group = page.locator(selector)
            count = await group.count()
            for index in range(count):
                candidate = group.nth(index)
                if await _matches_label(candidate, text):
                    chosen = candidate
                    break
        if await chosen.count() == 0:
            raise LookupError(f"radio group {selector} has no option {text!r}")
        await chosen.check()
        return "radio"

    locator = page.locator(selector).first
    if await locator.count() == 0:
        raise LookupError(f"selector {selector} matched nothing")

    if ftype == "select" or (options and ftype not in ("textarea",)):
        try:
            await locator.select_option(label=text)
        except Exception:
            try:
                await locator.select_option(text)
            except Exception:
                # Case-insensitive pass over the portal's own option labels /
                # values — "bachelor of science" vs "BSc", "yes" vs "Yes".
                matched = False
                option_nodes = locator.locator("option")
                for index in range(await option_nodes.count()):
                    option = option_nodes.nth(index)
                    label = (await option.evaluate("el => el.label || el.text || ''") or "").strip()
                    option_value = await option.evaluate("el => el.value")
                    want_cf = text.casefold()
                    if want_cf in (label.casefold(), str(option_value).casefold()):
                        await locator.select_option(str(option_value))
                        matched = True
                        break
                if not matched:
                    raise
        return "select"

    await locator.fill(text)
    return "fill"
