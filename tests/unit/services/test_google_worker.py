"""What the work thread may do in Google: the list is the guarantee."""

from __future__ import annotations

import pytest

from src.services import google_worker as worker

pytestmark = pytest.mark.anyio

USER = "user-1"


@pytest.mark.parametrize(
    "action",
    [
        "gmail_send",
        "send_message",
        "gmail_send_draft",
        "drive_share",
        "share_file",
        "gmail_create_filter",
        "contacts_delete",
        "anything",
    ],
)
async def test_what_speaks_to_someone_or_stays_is_refused(action):
    with pytest.raises(ValueError, match="Not allowed"):
        await worker.call(USER, action, {})


def test_no_allowed_action_sends_shares_or_makes_a_standing_rule():
    names = {function.__name__ for function in worker.ALLOWED.values()}
    assert not names & {
        "send_message",
        "send_draft",
        "share_file",
        "unshare_file",
        "create_filter",
        "delete_filter",
    }


@pytest.mark.parametrize("action", ["calendar_create", "calendar_update"])
async def test_an_invitation_is_refused(action):
    with pytest.raises(ValueError, match="Inviting"):
        await worker.call(USER, action, {"attendees": ["a@b.c"]})


async def test_it_cannot_act_for_another_user():
    with pytest.raises(ValueError, match="user_id"):
        await worker.call(USER, "drive_search", {"user_id": "someone-else"})


async def test_an_allowed_action_runs_for_the_user(monkeypatch):
    seen = {}

    async def run(user_id, **kwargs):
        seen.update(user_id=user_id, **kwargs)
        return "done"

    monkeypatch.setitem(worker.ALLOWED, "drive_search", run)
    assert await worker.call(USER, "drive_search", {"query": "x"}) == "done"
    assert seen == {"user_id": USER, "query": "x"}


async def test_wrong_arguments_are_a_refusal_not_a_crash():
    with pytest.raises(ValueError, match="Wrong arguments"):
        await worker.call(USER, "gmail_read", {"nope": 1})
