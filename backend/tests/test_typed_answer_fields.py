"""
Typed answer controls — a checkbox question must never reach a person as a
text box (and must never reach the page as an ``fill()`` call that cannot tick
a box).

Three contracts are pinned here:

* the field verdict carries the control's own ``type`` and ``options`` into the
  action payload (that is what the queue UI renders from);
* the typed-value executor ticks/unticks a checkbox, picks a radio option and
  selects a <select> option instead of ``fill()``-ing everything;
* a checkbox answer (JSON ``true``/``false``) survives as an explicit value
  ("no" is an answer), reaches the next pass as a typed instruction, and is
  learned like any other answer.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.models.models import User
from app.services import browser_session as sessions
from app.services import form_fill
from app.services.autofill import build_autofill_plan
from app.services.field_classifier import FieldVerdict, classify_field, classify_form


def run(coro):
    """The executor is async (real Playwright is); these tests drive fakes."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Field verdicts carry the control's own shape
# --------------------------------------------------------------------------- #
def test_verdict_as_dict_carries_type_and_options():
    verdict = FieldVerdict(
        name="work_auth", label="Are you authorized to work?", field_type="radio",
        required=True, classification="legal", action="ask_user",
        question="Are you authorized to work?", pause_kind="legal_question",
        options=("Yes", "No", "Yes, with sponsorship"),
    )
    payload = verdict.as_dict()
    assert payload["type"] == "radio"
    assert payload["options"] == ["Yes", "No", "Yes, with sponsorship"]
    assert payload["required"] is True


def test_checkbox_field_keeps_its_type_through_classification():
    field = {"name": "terms", "label": "I agree to the privacy policy", "type": "checkbox",
             "required": True}
    verdict = classify_field(field)
    payload = verdict.as_dict()
    # A legal consent checkbox is an ask_user pause — but as a *checkbox*.
    assert payload["type"] == "checkbox"
    assert payload["action"] == "ask_user"
    assert payload["options"] == []


def test_select_field_carries_the_portals_own_options():
    field = {"name": "pronouns", "label": "Pronouns", "type": "select", "required": True,
             "options": ["she/her", "he/him", "they/them", "Prefer not to say"]}
    verdict = classify_field(field)
    payload = verdict.as_dict()
    assert payload["type"] == "select"
    assert payload["options"] == ["she/her", "he/him", "they/them", "Prefer not to say"]


# --------------------------------------------------------------------------- #
# The typed-value executor (fake Playwright — duck-typed on purpose)
# --------------------------------------------------------------------------- #
class FakeLocator:
    def __init__(self, page, key: str):
        self.page = page
        self.key = key

    @property
    def first(self):
        return self

    async def count(self) -> int:
        if self.key in self.page.nodes:
            return int(self.page.nodes[self.key].get("count", 1))
        if "::" in self.key:  # option sub-locator
            parent, _sub = self.key.split("::", 1)
            return len(self.page.nodes.get(parent, {}).get("options", {}))
        if "#" in self.key and "[" not in self.key:  # nth child of a group
            group, index = self.key.rsplit("#", 1)
            node = self.page.nodes.get(group, {})
            return 1 if int(index) < int(node.get("count", 0)) else 0
        return 0

    async def check(self):
        self.page.calls.append(("check", self.key))

    async def uncheck(self):
        self.page.calls.append(("uncheck", self.key))

    async def fill(self, text, **_kwargs):
        self.page.calls.append(("fill", self.key, text))

    async def set_input_files(self, path):
        self.page.calls.append(("file", self.key, path))

    async def select_option(self, value=None, *, label=None, **_kwargs):
        node = self.page.nodes[self.key]
        for opt_label, opt_value in (node.get("options") or {}).items():
            if label is not None and opt_label == label:
                self.page.calls.append(("select", self.key, opt_value))
                return
            if label is None and str(opt_value) == str(value):
                self.page.calls.append(("select", self.key, opt_value))
                return
        raise ValueError(f"no matching option for {label or value!r}")

    async def evaluate(self, _script):
        # The label probe used for radio matching (labels[0].innerText).
        return self.page.nodes.get(self.key, {}).get("label", "")

    def locator(self, sub: str):
        return FakeLocator(self.page, f"{self.key}::{sub}")

    def nth(self, index: int):
        return FakeLocator(self.page, f"{self.key}#{index}")


class FakePage:
    """selector → node dict. ``[name="x"][value="y"]`` resolves inside a group."""

    def __init__(self, nodes: Dict[str, Dict[str, Any]]):
        self.nodes = nodes
        self.calls: List[Tuple[Any, ...]] = []

    def locator(self, selector: str):
        if selector not in self.nodes:
            # Radio option selector built by the executor: match the group's
            # child whose value attr is the picked option.
            for key, node in self.nodes.items():
                if node.get("kind") == "radio-group" and "[value=" in selector:
                    wanted = selector.split('[value="', 1)[1].rsplit('"]', 1)[0]
                    for child_label, child_value in (node.get("options") or {}).items():
                        if str(child_value) == wanted or child_label == wanted:
                            resolved = f"{key}[value=\"{wanted}\"]"
                            self.nodes.setdefault(resolved, {"count": 1, "label": child_label})
                            return FakeLocator(self, resolved)
        return FakeLocator(self, selector)


def test_checkbox_is_ticked_not_typed():
    page = FakePage({'[name="terms"]': {"count": 1}})
    action = run(form_fill.apply_field_value(
        page, '[name="terms"]', field_type="checkbox", value="true"))
    assert action == "checkbox"
    assert page.calls == [("check", '[name="terms"]')]


def test_checkbox_explicit_no_unticks():
    """'false' is the *answer* "no" — the box is unticked, never left alone."""
    page = FakePage({'[name="terms"]': {"count": 1}})
    run(form_fill.apply_field_value(
        page, '[name="terms"]', field_type="checkbox", value="false"))
    assert page.calls == [("uncheck", '[name="terms"]')]


def test_radio_picks_the_picked_option():
    page = FakePage({
        '[name="auth"]': {"kind": "radio-group", "count": 2,
                          "options": {"Yes": "yes", "No": "no"}, "label": ""},
    })
    action = run(form_fill.apply_field_value(
        page, '[name="auth"]', field_type="radio", value="No"))
    assert action == "radio"
    assert ("check", '[name="auth"][value="No"]') in page.calls


def test_select_takes_the_portals_option():
    page = FakePage({
        '[name="pronouns"]': {"count": 1,
                              "options": {"she/her": "f", "they/them": "t"}},
    })
    action = run(form_fill.apply_field_value(
        page, '[name="pronouns"]', field_type="select", value="they/them"))
    assert action == "select"
    assert page.calls == [("select", '[name="pronouns"]', "t")]


def test_text_fields_are_filled_and_missing_controls_raise():
    page = FakePage({'[name="nickname"]': {"count": 1}})
    action = run(form_fill.apply_field_value(
        page, '[name="nickname"]', field_type="text", value="Ada"))
    assert action == "fill"
    assert page.calls == [("fill", '[name="nickname"]', "Ada")]
    with pytest.raises(LookupError):
        run(form_fill.apply_field_value(
            page, '[name="nope"]', field_type="text", value="x"))


# --------------------------------------------------------------------------- #
# Answers: a checkbox "no" is a real answer
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_with_checkbox(db, owner):
    user = db.query(User).order_by(User.id).first()
    job = _job(db, user.id)
    session, _ = sessions.start_session(db, user=user, job=job)
    # What the pause carries to the queue UI: the checkbox question, typed.
    session.last_observation = {
        "url": job.url, "host": "jobs.lever.co", "title": "Apply",
        "fields": [{"name": "remote_ok", "label": "Are you open to remote work?",
                    "type": "checkbox", "required": True}],
    }
    db.commit()
    return user, session


def _job(db, user_id: int):
    from app.models.models import Job

    url = "https://jobs.lever.co/acme/typed-answers"
    job = Job(user_id=user_id, title="Engineer", company="Acme", url=url,
              external_id="typed-answers", dedupe_key=f"url:{url}", status="ready_to_apply")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_checkbox_false_survives_as_an_answer(db, session_with_checkbox):
    user, session = session_with_checkbox
    result = sessions.record_user_answers(db, session, {"remote_ok": False})
    assert result["accepted"] == ["remote_ok"]
    stored = session.checkpoint["fields"]["remote_ok"]
    assert stored["status"] == "answered"
    # The next pass types the *answer*, not an absence.
    values = sessions.session_values(session)
    assert values.get("remote_ok") == "false"
    instructions = sessions.fill_instructions(
        session,
        observations=[{"url": session.last_observation["url"], "fields": [
            {"name": "remote_ok", "label": "Are you open to remote work?", "type": "checkbox",
             "required": True}]}],
        values=values,
    )
    typed = [i for i in instructions if i["name"] == "remote_ok"]
    assert typed and typed[0]["value"] == "false"
    assert typed[0]["type"] == "checkbox"


def test_checkbox_true_is_answered_and_never_asked_again(db, session_with_checkbox):
    user, session = session_with_checkbox
    sessions.record_user_answers(db, session, {"remote_ok": True})
    form = classify_form(
        [{"name": "remote_ok", "label": "Are you open to remote work?", "type": "checkbox",
          "required": True}],
        values=sessions.session_values(session),
        checkpoint=session.checkpoint,
    )
    # answered + value present → autofill (typed on the next pass), never a pause.
    assert [v.name for v in form.autofillable] == ["remote_ok"]
    assert form.must_pause is False


def test_boolean_answers_are_normalised_for_action_rows(db, session_with_checkbox):
    user, session = session_with_checkbox
    sessions.record_user_answers(db, session, {"remote_ok": True})
    form = classify_form(
        [{"name": "remote_ok", "label": "Are you open to remote work?", "type": "checkbox",
          "required": True}],
        values={},
        checkpoint=session.checkpoint,
    )
    action = sessions.pause_session(db, session, kind="unknown_field", reason="x",
                                    fields=form.asking or form.blocking)
    assert action.id  # the action row exists and keeps field *metadata* only


def test_autofill_plan_missing_fields_carry_options():
    schema = {
        "portal_type": "custom",
        "fields": [
            {"name": "pronouns", "label": "Pronouns", "type": "select", "required": True,
             "options": ["she/her", "they/them"], "profile_key": "pronouns"},
            {"name": "full_name", "label": "Full name", "type": "text", "required": True,
             "options": [], "profile_key": "fullName"},
        ],
    }
    plan = build_autofill_plan(schema=schema, profile={}, answers={})
    missing = {m["name"]: m for m in plan["missing_required"]}
    assert missing["pronouns"]["options"] == ["she/her", "they/them"]
    assert missing["pronouns"]["type"] == "select"
    assert missing["full_name"]["options"] == []
