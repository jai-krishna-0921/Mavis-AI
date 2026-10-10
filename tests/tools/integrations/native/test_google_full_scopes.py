# ruff: noqa: E501
"""Full Workspace read and write: mail triage, calendars, contacts, Slides, Forms, Meet.

Each action runs against a fake Google shaped like the real API; results go through the renderer the model
reads, and the retry flag of every write is proven by what happens on a 5xx."""

from __future__ import annotations

import json

import httpx
import pytest

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.domain.policy import RiskClass
from mavis.tools.integrations import mail_render as mr
from mavis.tools.integrations import workspace_render as wr
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.native.slides_outline import parse_outline, slide_requests
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.registry import ToolRegistry

from .conftest import *  # noqa: F403 - fixtures
from .gfake import FakeGoogle, body_of, error

USER = UserRef(user_id=9)
BATCH_MODIFY = r"/gmail/v1/users/me/messages/batchModify$"
LABELS = r"/gmail/v1/users/me/labels$"
LABEL_LIST = {"labels": [
    {"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"}, {"id": "Label_7", "name": "Receipts", "type": "user"}]}


def ok(payload=None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {})


async def run(fake: FakeGoogle, action: str, args: dict):
    ex, sleeps = fake.executor()
    return await ex.execute(USER, action, args), sleeps


# --- Gmail: archive, read state, labels, trash -----------------------------------------------------------


@pytest.mark.parametrize(("action", "add", "remove"), [
    ("mail.archive", [], ["INBOX"]),
    ("mail.mark_read", [], ["UNREAD"]),
    ("mail.mark_unread", ["UNREAD"], []),
])
async def test_triage_actions_are_one_batch_modify_for_all_messages(action, add, remove):
    fake = FakeGoogle().on("POST", BATCH_MODIFY, ok())
    res, _ = await run(fake, action, {"message_ids": ["m1", "m2", "m3"]})
    assert res.ok and len(fake.requests) == 1
    assert body_of(fake.requests[0]) == {"ids": ["m1", "m2", "m3"], "addLabelIds": add, "removeLabelIds": remove}
    text = mr.render_change(res.data)
    assert text.startswith("Done.") and "3 email(s)" in text


async def test_archiving_a_thread_modifies_the_thread():
    fake = FakeGoogle().on("POST", r"/threads/t1/modify$", ok())
    res, _ = await run(fake, "mail.archive", {"thread_ids": ["t1"]})
    assert res.ok and body_of(fake.requests[0])["removeLabelIds"] == ["INBOX"]
    assert "1 conversation(s)" in mr.render_change(res.data)


async def test_triage_needs_at_least_one_id():
    res, _ = await run(FakeGoogle(), "mail.archive", {})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT


async def test_an_id_that_could_address_another_endpoint_is_refused_in_a_list():
    fake = FakeGoogle()
    res, _ = await run(fake, "mail.archive", {"message_ids": ["ok", "../../settings"]})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT and fake.requests == []


async def test_label_modifications_are_repeated_after_a_5xx_because_they_set_a_state():
    fake = FakeGoogle().on("POST", BATCH_MODIFY, [error(503, "backendError"), ok()])
    res, sleeps = await run(fake, "mail.archive", {"message_ids": ["m1"]})
    assert res.ok and len(fake.requests) == 2 and sleeps


async def test_label_add_resolves_names_case_insensitively_and_creates_missing_labels_once():
    fake = (FakeGoogle().on("GET", LABELS, ok(LABEL_LIST))
            .on("POST", LABELS, ok({"id": "Label_9", "name": "Taxes"}))
            .on("POST", BATCH_MODIFY, ok()))
    res, _ = await run(fake, "mail.label", {"message_ids": ["m1"], "add": ["receipts", "Taxes"],
                                            "remove": ["inbox"]})
    assert res.ok
    created = fake.calls("POST", LABELS)
    assert len(created) == 1 and body_of(created[0])["name"] == "Taxes"
    assert body_of(fake.calls("POST", BATCH_MODIFY)[0]) == {
        "ids": ["m1"], "addLabelIds": ["Label_7", "Label_9"], "removeLabelIds": ["INBOX"]}


async def test_removing_a_label_that_does_not_exist_is_refused_not_created():
    fake = FakeGoogle().on("GET", LABELS, ok(LABEL_LIST))
    res, _ = await run(fake, "mail.label", {"message_ids": ["m1"], "remove": ["Nope"]})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "remove"
    assert fake.calls("POST", LABELS) == []


@pytest.mark.parametrize("name", ["TRASH", "trash", "SENT", "DRAFT"])
async def test_a_label_change_cannot_be_used_to_trash_around_the_approval(name):
    fake = FakeGoogle().on("GET", LABELS, ok(LABEL_LIST))
    res, _ = await run(fake, "mail.label", {"message_ids": ["m1"], "add": [name]})
    assert not res.ok and res.error_field == "add" and fake.calls("POST", BATCH_MODIFY) == []


async def test_label_with_nothing_given_lists_labels_and_is_a_read():
    spec = ACTIONS["mail.label"]
    from mavis.tools.integrations.actions import MailLabelArgs

    assert spec.risk_for(MailLabelArgs()) is RiskClass.READ
    assert spec.risk_for(MailLabelArgs(message_ids=["m"], add=["x"])) is RiskClass.WRITE_SELF
    fake = FakeGoogle().on("GET", LABELS, ok(LABEL_LIST))
    res, _ = await run(fake, "mail.label", {})
    text = mr.render_labels(res.data)
    assert res.ok and "Receipts (id Label_7)" in text and "INBOX" in text


def test_label_args_reject_half_requests():
    from mavis.tools.integrations.actions import MailLabelArgs

    with pytest.raises(ValueError):
        MailLabelArgs(add=["x"])  # labels but no mail
    with pytest.raises(ValueError):
        MailLabelArgs(message_ids=["m"])  # mail but no labels


async def test_trash_and_untrash_call_the_per_message_endpoints_and_are_repeatable():
    fake = FakeGoogle().on("POST", r"/messages/m1/trash$", [error(503, "backendError"), ok({"id": "m1"})])
    fake.on("POST", r"/threads/t9/trash$", ok({"id": "t9"}))
    res, _ = await run(fake, "mail.trash", {"message_ids": ["m1"], "thread_ids": ["t9"]})
    assert res.ok and len(fake.calls("POST", r"/messages/m1/trash$")) == 2  # state-setting: safe to repeat
    assert mr.render_change(res.data) == "Done. Moved to Trash 1 email(s) and 1 conversation(s)."
    back = FakeGoogle().on("POST", r"/messages/m1/untrash$", ok({"id": "m1"}))
    res, _ = await run(back, "mail.untrash", {"message_ids": ["m1"]})
    assert res.ok and "Restored from Trash" in mr.render_change(res.data)


def test_risk_classes_of_the_mail_actions():
    assert ACTIONS["mail.trash"].risk is RiskClass.DESTRUCTIVE and ACTIONS["mail.trash"].preview
    for name in ("mail.archive", "mail.mark_read", "mail.mark_unread", "mail.label", "mail.untrash"):
        assert ACTIONS[name].risk is RiskClass.WRITE_SELF, name


# --- Calendar ---------------------------------------------------------------------------------------------


async def test_calendars_lists_own_and_shared_calendars():
    fake = FakeGoogle().on("GET", r"/calendar/v3/users/me/calendarList$", ok({"items": [
        {"id": "me@x.com", "summary": "Me", "primary": True, "accessRole": "owner", "timeZone": "Asia/Kolkata"},
        {"id": "team@group.calendar.google.com", "summary": "Team", "accessRole": "reader"}]}))
    res, _ = await run(fake, "calendar.calendars", {})
    text = wr.render_calendars(res.data)
    assert res.ok and "calendar_id=me@x.com" in text and "(primary)" in text
    assert "calendar_id=team@group.calendar.google.com" in text and "access: reader" in text


async def test_list_and_find_default_to_primary_and_accept_another_calendar():
    fake = FakeGoogle().on("GET", r"/calendar/v3/calendars/[^/]+/events$", ok({"items": []}))
    ex, _ = fake.executor()
    window = {"time_min": "2026-10-06T00:00:00+00:00", "time_max": "2026-10-07T00:00:00+00:00"}
    await ex.execute(USER, "calendar.list", window)
    await ex.execute(USER, "calendar.list", {**window, "calendar_id": "team@group.calendar.google.com"})
    await ex.execute(USER, "calendar.find", {"query": "sync", "calendar_id": "team@group.calendar.google.com"})
    paths = [r.url.raw_path.decode().split("?")[0] for r in fake.requests]
    assert paths == ["/calendar/v3/calendars/primary/events",
                     "/calendar/v3/calendars/team%40group.calendar.google.com/events",
                     "/calendar/v3/calendars/team%40group.calendar.google.com/events"]


async def test_delete_event_notifies_guests_only_when_the_user_agreed():
    fake = FakeGoogle().on("DELETE", r"/events/e1$", httpx.Response(204))
    res, _ = await run(fake, "calendar.delete_event", {"event_id": "e1"})
    assert res.ok and fake.requests[0].url.params["sendUpdates"] == "none"
    fake = FakeGoogle().on("DELETE", r"/events/e1$", httpx.Response(204))
    res, _ = await run(fake, "calendar.delete_event", {"event_id": "e1", "notify_guests": True})
    assert res.ok and fake.requests[0].url.params["sendUpdates"] == "all" and res.data["guestsNotified"]


async def test_delete_event_is_always_an_approval_and_the_preview_says_who_hears():
    from mavis.tools.integrations.actions import CalendarDeleteArgs

    spec = ACTIONS["calendar.delete_event"]
    assert spec.risk is RiskClass.DESTRUCTIVE and spec.risk.needs_approval
    assert "not notified" in spec.preview(CalendarDeleteArgs(event_id="e1"), "UTC")
    assert "told it is cancelled" in spec.preview(CalendarDeleteArgs(event_id="e1", notify_guests=True), "UTC")


async def test_respond_changes_only_my_own_attendee_row_and_tells_the_organiser():
    event = {"id": "e1", "summary": "Sync", "attendees": [
        {"email": "boss@x.com", "organizer": True, "responseStatus": "accepted"},
        {"email": "me@x.com", "self": True, "responseStatus": "needsAction"},
        {"email": "other@x.com", "responseStatus": "declined"}]}
    fake = (FakeGoogle().on("GET", r"/events/e1$", ok(event))
            .on("PATCH", r"/events/e1$", ok({**event, "summary": "Sync"})))
    res, _ = await run(fake, "calendar.respond", {"event_id": "e1", "response": "declined", "comment": "Clash"})
    assert res.ok
    patch = fake.calls("PATCH", r"/events/e1$")[0]
    sent = body_of(patch)["attendees"]
    assert [g["responseStatus"] for g in sent] == ["accepted", "declined", "declined"]
    assert sent[1]["comment"] == "Clash" and sent[0].get("comment") is None
    assert patch.url.params["sendUpdates"] == "all"


async def test_respond_to_an_event_i_am_not_invited_to_is_a_clear_error():
    fake = FakeGoogle().on("GET", r"/events/e1$", ok({"id": "e1", "attendees": [{"email": "a@x.com"}]}))
    res, _ = await run(fake, "calendar.respond", {"event_id": "e1", "response": "accepted"})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT
    assert fake.calls("PATCH", r"/events/e1$") == []


async def test_respond_is_not_repeated_after_a_5xx_because_the_organiser_would_hear_twice():
    event = {"id": "e1", "attendees": [{"email": "me@x.com", "self": True}]}
    fake = (FakeGoogle().on("GET", r"/events/e1$", ok(event))
            .on("PATCH", r"/events/e1$", error(503, "backendError")))
    res, _ = await run(fake, "calendar.respond", {"event_id": "e1", "response": "tentative"})
    assert not res.ok and res.error_kind is FailureKind.UNCONFIRMED
    assert len(fake.calls("PATCH", r"/events/e1$")) == 1


def test_respond_is_outward_with_a_preview():
    spec = ACTIONS["calendar.respond"]
    assert spec.risk is RiskClass.OUTWARD and spec.preview


# --- Contacts ----------------------------------------------------------------------------------------------

PERSON = {"names": [{"displayName": "Priya Nair"}], "emailAddresses": [{"value": "priya@x.com"}],
          "resourceName": "people/c1"}


async def test_search_covers_saved_other_and_directory_and_dedupes_by_email():
    fake = (FakeGoogle()
            .on("GET", r"/people:searchContacts$", ok({"results": [{"person": PERSON}]}))
            .on("GET", r"/otherContacts:search$", ok({"results": [
                {"person": {"names": [{"displayName": "Priya N"}], "emailAddresses": [{"value": "PRIYA@x.com"}]}},
                {"person": {"names": [{"displayName": "Dev"}], "emailAddresses": [{"value": "dev@x.com"}]}}]}))
            .on("GET", r"/people:searchDirectoryPeople$", ok({"people": [
                {"names": [{"displayName": "Priya Nair"}], "emailAddresses": [{"value": "priya@work.com"}]}]})))
    res, _ = await run(fake, "contacts.search", {"query": "priya"})
    text = wr.render_contacts(res.data)
    assert res.ok and "not_searched" not in res.data
    assert text.count("Priya N") == 2 and "dev@x.com" in text and "from your email history" in text
    assert "company directory" in text and "id: people/c1" in text
    directory = fake.calls("GET", r"/people:searchDirectoryPeople$")[0].url.params.get_list("sources")
    assert "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE" in directory


@pytest.mark.parametrize("failure", [
    error(403, "forbidden", "Request had insufficient authentication scopes."),
    error(400, "failedPrecondition", "Must be a Google Workspace user"),
    error(404, "notFound", "no directory"),
    error(503, "backendError"),
])
async def test_directory_and_other_contact_errors_are_not_failures(failure):
    fake = (FakeGoogle().on("GET", r"/people:searchContacts$", ok({"results": [{"person": PERSON}]}))
            .on("GET", r"/otherContacts:search$", failure)
            .on("GET", r"/people:searchDirectoryPeople$", failure))
    res, _ = await run(fake, "contacts.search", {"query": "priya"})
    assert res.ok and res.data["not_searched"] == ["other contacts", "company directory"]
    assert "Priya Nair" in wr.render_contacts(res.data)


async def test_an_empty_personal_search_says_what_was_not_searched():
    fake = (FakeGoogle().on("GET", r"/people:searchContacts$", ok({}))
            .on("GET", r"/otherContacts:search$", ok({}))
            .on("GET", r"/people:searchDirectoryPeople$", error(400, "failedPrecondition", "no")))
    res, _ = await run(fake, "contacts.search", {"query": "zzz"})
    assert wr.render_contacts(res.data) == "No contacts matched. (not searched: company directory)"


async def test_saved_contact_search_errors_are_still_real_errors():
    fake = FakeGoogle().on("GET", r"/people:searchContacts$", error(400, "invalidArgument", "bad query"))
    res, _ = await run(fake, "contacts.search", {"query": "zz"})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT


async def test_create_contact_posts_once_and_is_not_repeated():
    fake = FakeGoogle().on("POST", r"/people:createContact$", ok({**PERSON, "resourceName": "people/c9"}))
    res, _ = await run(fake, "contacts.create", {"name": "Priya Nair", "emails": ["priya@x.com"],
                                                 "phones": ["+91 99999"], "organization": "Acme"})
    assert res.ok and res.data["resourceName"] == "people/c9"
    sent = body_of(fake.requests[0])
    assert sent["names"] == [{"givenName": "Priya", "familyName": "Nair"}]
    assert sent["emailAddresses"] == [{"value": "priya@x.com"}] and sent["organizations"] == [{"name": "Acme"}]
    down = FakeGoogle().on("POST", r"/people:createContact$", error(503, "backendError"))
    res, _ = await run(down, "contacts.create", {"name": "A B", "emails": ["a@x.com"]})
    assert res.error_kind is FailureKind.UNCONFIRMED and len(down.requests) == 1


async def test_update_contact_reads_the_etag_and_masks_only_the_fields_given():
    fake = (FakeGoogle().on("GET", r"/v1/people/c1$", ok({**PERSON, "etag": "E1"}))
            .on("PATCH", r"/v1/people/c1:updateContact$", ok(PERSON)))
    res, _ = await run(fake, "contacts.update", {"resource_name": "people/c1", "phones": ["+1 555"]})
    assert res.ok
    patch = fake.calls("PATCH", r":updateContact$")[0]
    assert patch.url.params["updatePersonFields"] == "phoneNumbers"
    assert body_of(patch) == {"phoneNumbers": [{"value": "+1 555"}], "etag": "E1"}


def test_contact_args_are_validated():
    from mavis.tools.integrations.actions import ContactCreateArgs, ContactUpdateArgs

    with pytest.raises(ValueError):
        ContactCreateArgs(name="No Way")  # nothing to reach them by
    with pytest.raises(ValueError):
        ContactUpdateArgs(resource_name="people/c1")  # nothing to change
    with pytest.raises(ValueError):
        ContactUpdateArgs(resource_name="people/c1/../x", name="A")


# --- Slides ----------------------------------------------------------------------------------------------

DECK = {"presentationId": "P1", "title": "Plan", "slides": [
    {"objectId": "s1", "pageElements": [
        {"objectId": "a", "shape": {"text": {"textElements": [{"textRun": {"content": "Roadmap\n"}}]}}},
        {"objectId": "b", "shape": {"text": {"textElements": [
            {"textRun": {"content": "Ship v2\n"}}, {"paragraphMarker": {}}, {"textRun": {"content": "Hire\n"}}]}}}]},
    {"objectId": "s2", "pageElements": [{"objectId": "t", "table": {"tableRows": [{"tableCells": [
        {"text": {"textElements": [{"textRun": {"content": "Q1"}}]}},
        {"text": {"textElements": [{"textRun": {"content": "10"}}]}}]}]}}]}]}


async def test_slides_read_renders_the_text_of_each_slide():
    fake = FakeGoogle().on("GET", r"/v1/presentations/P1$", ok(DECK))
    res, _ = await run(fake, "slides.read", {"presentation_id": "P1"})
    text = wr.render_slides(res.data)
    assert res.ok and "Plan" in text and "2 slide(s)" in text
    assert "--- Slide 1 ---\nRoadmap\n" in text and "Ship v2" in text and "Q1 | 10" in text


def test_outline_rule_headings_are_slides_and_lines_below_are_the_body():
    slides = parse_outline("Intro line\nmore\n\n# Goals\n- Grow **fast**\n  - sub point\n\n## Risks\n")
    assert [s.title for s in slides] == ["Intro line", "Goals", "Risks"]
    assert slides[0].body == "more" and slides[1].body == "Grow fast\n\tsub point" and slides[2].body == ""
    assert parse_outline("") == []


def test_outline_requests_use_title_only_layout_for_empty_bodies_and_stable_ids():
    reqs = slide_requests(parse_outline("# A\nbody\n# B"))
    layouts = [r["createSlide"]["slideLayoutReference"]["predefinedLayout"] for r in reqs if "createSlide" in r]
    assert layouts == ["TITLE_AND_BODY", "TITLE_ONLY"]
    assert {"insertText": {"objectId": "mavis_s1_b", "text": "body"}} in reqs


async def test_slides_create_builds_the_deck_in_one_batch_and_drops_the_blank_first_slide():
    fake = (FakeGoogle().on("POST", r"/v1/presentations$", ok({
        "presentationId": "P2", "title": "Plan", "slides": [{"objectId": "blank"}]}))
            .on("POST", r"/v1/presentations/P2:batchUpdate$", ok({})))
    res, _ = await run(fake, "slides.create", {"title": "Plan", "outline": "# One\nhello\n# Two\nworld"})
    assert res.ok and res.data["presentationId"] == "P2" and res.data["slides"] == 2
    reqs = body_of(fake.calls("POST", r":batchUpdate$")[0])["requests"]
    assert sum("createSlide" in r for r in reqs) == 2 and reqs[-1] == {"deleteObject": {"objectId": "blank"}}
    assert wr.render_created(res.data).startswith("Done.") and "P2" in wr.render_created(res.data)


async def test_slides_create_is_sent_once_and_a_failed_fill_trashes_the_empty_deck():
    fake = (FakeGoogle().on("POST", r"/v1/presentations$", ok({"presentationId": "P3", "slides": []}))
            .on("POST", r":batchUpdate$", error(400, "invalidArgument", "bad request"))
            .on("PATCH", r"/drive/v3/files/P3$", ok({"id": "P3"})))
    res, _ = await run(fake, "slides.create", {"title": "T", "outline": "# A"})
    assert not res.ok and body_of(fake.calls("PATCH", r"/files/P3$")[0]) == {"trashed": True}
    flaky = (FakeGoogle().on("POST", r"/v1/presentations$", ok({"presentationId": "P4", "slides": []}))
             .on("POST", r":batchUpdate$", error(503, "backendError")))
    res, _ = await run(flaky, "slides.create", {"title": "T", "outline": "# A"})
    assert res.error_kind is FailureKind.UNCONFIRMED and "P4" in res.error
    assert len(flaky.calls("POST", r":batchUpdate$")) == 1


def test_slides_create_is_write_self_and_runs_without_a_card():
    """A deck in the user's own Drive needs no tap, even after third-party content (owner, 2026-10-10)."""
    spec = ACTIONS["slides.create"]
    assert spec.risk is RiskClass.WRITE_SELF and not spec.taint_approve and spec.preview


# --- Forms -----------------------------------------------------------------------------------------------

FORM = {"formId": "F1", "info": {"title": "Lunch poll", "description": "Pick one"}, "items": [
    {"itemId": "i1", "title": "Where?", "questionItem": {"question": {
        "questionId": "q1", "required": True, "choiceQuestion": {"options": [{"value": "Thai"}, {"value": "Pizza"}]}}}},
    {"itemId": "i2", "title": "Comments", "questionItem": {"question": {"questionId": "q2", "textQuestion": {}}}}]}


def answers(q1: str, q2: str) -> dict:
    return {"answers": {"q1": {"questionId": "q1", "textAnswers": {"answers": [{"value": q1}]}},
                        "q2": {"questionId": "q2", "textAnswers": {"answers": [{"value": q2}]}}}}


async def test_forms_read_lists_questions_and_choices():
    fake = FakeGoogle().on("GET", r"/v1/forms/F1$", ok(FORM))
    res, _ = await run(fake, "forms.read", {"form_id": "F1"})
    text = wr.render_form(res.data)
    assert "Lunch poll" in text and "1. Where? (required) | choice | options: Thai, Pizza" in text
    assert "2. Comments | text" in text


async def test_forms_responses_tally_choices_and_sample_text_and_cap():
    fake = (FakeGoogle().on("GET", r"/v1/forms/F1$", ok(FORM))
            .on("GET", r"/v1/forms/F1/responses$", ok({"responses": [
                answers("Thai", "great"), answers("Thai", "ok"), answers("Pizza", "meh")]})))
    res, _ = await run(fake, "forms.responses", {"form_id": "F1", "max_responses": 2})
    assert res.ok and len(res.data["responses"]) == 2 and res.data["more"]
    assert fake.calls("GET", r"/responses$")[0].url.params["pageSize"] == "2"
    full = FakeGoogle().on("GET", r"/v1/forms/F1$", ok(FORM)).on("GET", r"/responses$", ok({"responses": [
        answers("Thai", "great"), answers("Thai", "ok"), answers("Pizza", "meh")]}))
    res, _ = await run(full, "forms.responses", {"form_id": "F1"})
    text = wr.render_form_responses(res.data)
    assert "3 response(s)" in text and "Where? (3 answers) | Thai: 2; Pizza: 1" in text
    assert "Comments (3 answers) | e.g. great / ok / meh" in text


async def test_forms_with_no_responses_say_so():
    fake = FakeGoogle().on("GET", r"/v1/forms/F1$", ok(FORM)).on("GET", r"/responses$", ok({}))
    res, _ = await run(fake, "forms.responses", {"form_id": "F1"})
    assert "No responses yet." in wr.render_form_responses(res.data)


# --- Meet -------------------------------------------------------------------------------------------------


async def test_recent_meetings_list_with_participants_newest_first():
    fake = (FakeGoogle()
            .on("GET", r"/v2/conferenceRecords$", ok({"conferenceRecords": [
                {"name": "conferenceRecords/old", "startTime": "2026-10-01T09:00:00Z", "endTime": "2026-10-01T10:00:00Z"},
                {"name": "conferenceRecords/new", "startTime": "2026-10-08T09:00:00Z", "endTime": "2026-10-08T09:30:00Z"}]}))
            .on("GET", r"/conferenceRecords/new/participants$", ok({"participants": [
                {"name": "conferenceRecords/new/participants/p1", "signedinUser": {"user": "users/1", "displayName": "Asha"}},
                {"name": "conferenceRecords/new/participants/p2", "anonymousUser": {"displayName": "Guest 4"}}]}))
            .on("GET", r"/conferenceRecords/old/participants$", error(403, "forbidden", "no")))
    res, _ = await run(fake, "meet.recent", {"days": 30})
    text = wr.render_meet_recent(res.data)
    lines = text.splitlines()
    assert "conference_record_id=new" in lines[1] and "Asha, Guest 4" in lines[1]
    assert "conference_record_id=old" in lines[2] and "with: unknown" in lines[2]
    assert fake.calls("GET", r"/v2/conferenceRecords$")[0].url.params["filter"].startswith('start_time>="')


async def test_recent_meetings_with_none_says_so_plainly():
    fake = FakeGoogle().on("GET", r"/v2/conferenceRecords$", ok({}))
    res, _ = await run(fake, "meet.recent", {})
    assert res.ok and wr.render_meet_recent(res.data) == "No Google Meet calls found in that period."


async def test_transcript_returns_spoken_text_by_speaker_and_the_doc_link():
    fake = (FakeGoogle()
            .on("GET", r"/conferenceRecords/c1/transcripts$", ok({"transcripts": [{
                "name": "conferenceRecords/c1/transcripts/t1", "state": "FILE_GENERATED",
                "docsDestination": {"document": "DOC1"}}]}))
            .on("GET", r"/conferenceRecords/c1/participants$", ok({"participants": [
                {"name": "conferenceRecords/c1/participants/p1", "signedinUser": {"displayName": "Asha"}}]}))
            .on("GET", r"/transcripts/t1/entries$", ok({"transcriptEntries": [
                {"participant": "conferenceRecords/c1/participants/p1", "text": "Let's ship Friday.",
                 "startTime": "2026-10-08T09:01:00Z"},
                {"participant": "conferenceRecords/c1/participants/p9", "text": "Agreed."}]})))
    res, _ = await run(fake, "meet.transcript", {"conference_record_id": "c1"})
    text = wr.render_transcripts(res.data)
    assert res.ok and "document_id=DOC1" in text
    assert "Asha: Let's ship Friday." in text and "Participant: Agreed." in text


async def test_no_transcript_is_a_plain_answer_not_an_error():
    fake = FakeGoogle().on("GET", r"/transcripts$", ok({}))
    res, _ = await run(fake, "meet.transcript", {"conference_record_id": "c1"})
    assert res.ok and wr.render_transcripts(res.data).startswith("No transcript is available")


async def test_transcript_entries_are_capped():
    entries = [{"participant": "x", "text": f"line {i}"} for i in range(100)]
    fake = (FakeGoogle().on("GET", r"/conferenceRecords/c1/transcripts$", ok({"transcripts": [{
        "name": "conferenceRecords/c1/transcripts/t1", "state": "ENDED"}]}))
            .on("GET", r"/conferenceRecords/c1/participants$", ok({}))
            .on("GET", r"/entries$", ok({"transcriptEntries": entries, "nextPageToken": "n"})))
    res, _ = await run(fake, "meet.transcript", {"conference_record_id": "c1"})
    assert res.ok and len(res.data["transcripts"][0]["entries"]) <= 400


# --- the model picks the right tool ------------------------------------------------------------------------


@pytest.mark.parametrize(("ask", "tool"), [
    ("archive this email", "mail_archive"),
    ("mark these emails as read", "mail_mark_read"),
    ("mark that email as unread", "mail_mark_unread"),
    ("label this email as receipts", "mail_label"),
    ("delete these emails and move them to trash", "mail_trash"),
    ("restore that email from trash", "mail_untrash"),
    ("delete this event from my calendar", "calendar_delete_event"),
    ("accept the invite for tomorrow", "calendar_respond"),
    ("show all my calendars including shared ones", "calendar_calendars"),
    ("what's in my slides", "slides_read"),
    ("make a slides deck about our roadmap", "slides_create"),
    ("save Priya as a contact", "contacts_create"),
    ("update the phone number of this contact", "contacts_update"),
    ("how did people answer my form", "forms_responses"),
    ("what questions are in this form", "forms_read"),
    ("what was said in my last meet call", "meet_transcript"),
    ("show my recent meet calls", "meet_recent"),
])
def test_a_plain_request_offers_the_right_tool(workspace_on, ask, tool):
    registry = ToolRegistry()
    register_integration_tools(registry)
    chosen = [t.name for t in registry.select("conversation", 1, ask, limit=4)]
    assert tool in chosen, (ask, chosen)


def test_every_new_action_has_a_scope_entry_and_a_renderer_or_a_plain_result():
    from mavis.tools.integrations.native.router import ACTION_SCOPES

    new = {"mail.archive", "mail.mark_read", "mail.mark_unread", "mail.label", "mail.trash", "mail.untrash",
           "calendar.calendars", "calendar.get", "calendar.delete_event", "calendar.respond",
           "contacts.create", "contacts.update", "slides.read", "slides.create", "forms.read",
           "forms.responses", "meet.recent"}
    assert new <= set(ACTION_SCOPES) and new <= set(ACTIONS)
    for name in new:
        spec = ACTIONS[name]
        assert (spec.risk.needs_approval or spec.risk_fn is not None) <= bool(spec.preview), name


def test_no_dashes_in_new_user_facing_text():
    from mavis.tools.integrations.actions import (
        CalendarDeleteArgs,
        CalendarRespondArgs,
        ContactCreateArgs,
        MailIdsArgs,
        MailLabelArgs,
        SlidesCreateArgs,
    )

    samples = [
        ACTIONS["mail.trash"].preview(MailIdsArgs(message_ids=["a"], thread_ids=["b"]), "UTC"),
        ACTIONS["mail.untrash"].preview(MailIdsArgs(message_ids=["a"]), "UTC"),
        ACTIONS["mail.label"].preview(MailLabelArgs(message_ids=["a"], add=["x"], remove=["y"]), "UTC"),
        ACTIONS["calendar.delete_event"].preview(CalendarDeleteArgs(event_id="e"), "UTC"),
        ACTIONS["calendar.respond"].preview(CalendarRespondArgs(event_id="e", response="accepted"), "UTC"),
        ACTIONS["contacts.create"].preview(ContactCreateArgs(name="A B", emails=["a@x.com"]), "UTC"),
        ACTIONS["slides.create"].preview(SlidesCreateArgs(title="T", outline="# A"), "UTC"),
        wr.NO_TRANSCRIPT,
        *(ACTIONS[n].description for n in ACTIONS if n.split(".")[0] in {"mail", "calendar", "slides", "forms", "meet"}),
    ]
    assert not any(d in text for text in samples for d in ("—", "–"))
    assert json.dumps(samples)
