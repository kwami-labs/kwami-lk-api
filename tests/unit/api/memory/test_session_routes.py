"""`/memory/{user_id}/sessions` and `/status`: conversations kept whole.

The fake `thread.get` answers like Zep does (measured 2026-11-06): `limit=n`
returns the first n messages with `total_count`; `lastn=n` the last n, without
a total; `cursor` is a 1-based page; the answer names the thread's owner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.memory import threads as threads_mod
from tests.unit.api.memory.conftest import USER_ID, thread

pytestmark = pytest.mark.anyio

BASE = f"/memory/{USER_ID}"


def msg(content="hi", role="user", created_at="2026-03-01T10:00:00Z", uuid=None, name=None):
    return SimpleNamespace(
        uuid_=uuid or f"m-{content}",
        content=content,
        role=role,
        role_type=None,
        name=name,
        created_at=created_at,
    )


class FakeThreads:
    """Per-thread transcripts behind `thread.get`, answered the way Zep answers."""

    def __init__(self, zep, transcripts: dict[str, list], owner: str | None = USER_ID):
        self.transcripts = transcripts
        self.owner = owner
        self.calls: list[dict] = []
        zep.thread.get = self

    async def __call__(self, *, thread_id, limit=None, lastn=None, cursor=None):
        self.calls.append(
            {"thread_id": thread_id, "limit": limit, "lastn": lastn, "cursor": cursor}
        )
        messages = self.transcripts.get(thread_id)
        if messages is None:
            raise RuntimeError("status_code: 404, thread not found")
        if lastn is not None:
            return SimpleNamespace(messages=messages[-lastn:], total_count=0, user_id=self.owner)
        page = (cursor or 1) - 1
        window = messages[page * limit : (page + 1) * limit]
        return SimpleNamespace(messages=window, total_count=len(messages), user_id=self.owner)


def two_sessions(zep):
    zep.user.get_threads.result = [
        thread("t-old", created_at="2026-03-01T09:00:00Z"),
        thread("t-new", created_at="2026-03-05T09:00:00Z"),
    ]
    return FakeThreads(
        zep,
        {
            "t-old": [
                msg("Hola", "assistant", "2026-03-01T09:00:05Z", name="Jarvis"),
                msg("Quiero  planear\nel viaje a Lisboa", "user", "2026-03-01T09:00:10Z"),
                msg("Claro", "assistant", "2026-03-01T09:00:20Z"),
                msg("Gracias", "user", "2026-03-01T09:30:00Z"),
            ],
            "t-new": [msg("Buenos días", "user", "2026-03-05T09:00:03Z")],
        },
    )


class TestListSessions:
    PATH = f"{BASE}/sessions"

    async def test_sessions_are_newest_first_with_their_shape(self, mem, zep):
        two_sessions(zep)
        body = (await mem.get(self.PATH)).json()
        assert [s["thread_id"] for s in body["sessions"]] == ["t-new", "t-old"]
        old = body["sessions"][1]
        assert old["started_at"] == "2026-03-01T09:00:05Z"
        assert old["ended_at"] == "2026-03-01T09:30:00Z"
        assert old["message_count"] == 4
        assert old["preview"] == "Quiero planear el viaje a Lisboa"
        assert old["empty"] is False
        assert body["total_threads"] == 2
        assert body["next_offset"] is None

    async def test_a_short_session_is_described_in_one_call(self, mem, zep):
        """Its first page already holds its last message."""
        fake = two_sessions(zep)
        await mem.get(self.PATH, params={"limit": 1})
        assert fake.calls == [{"thread_id": "t-new", "limit": 4, "lastn": None, "cursor": None}]

    async def test_a_long_session_also_reads_its_last_message(self, mem, zep):
        zep.user.get_threads.result = [thread("t-long")]
        fake = FakeThreads(
            zep,
            {"t-long": [msg(f"m{i}", created_at=f"2026-03-01T09:0{i}:00Z") for i in range(6)]},
        )
        body = (await mem.get(self.PATH)).json()
        assert fake.calls == [
            {"thread_id": "t-long", "limit": 4, "lastn": None, "cursor": None},
            {"thread_id": "t-long", "limit": None, "lastn": 1, "cursor": None},
        ]
        assert body["sessions"][0]["ended_at"] == "2026-03-01T09:05:00Z"
        assert body["sessions"][0]["message_count"] == 6

    async def test_an_empty_session_costs_one_call(self, mem, zep):
        zep.user.get_threads.result = [thread("t-empty")]
        fake = FakeThreads(zep, {"t-empty": []})
        await mem.get(self.PATH)
        assert len(fake.calls) == 1

    async def test_a_page_continues_from_next_offset(self, mem, zep):
        two_sessions(zep)
        first = (await mem.get(self.PATH, params={"limit": 1})).json()
        assert first["next_offset"] == 1
        second = (await mem.get(self.PATH, params={"limit": 1, "offset": 1})).json()
        assert [s["thread_id"] for s in second["sessions"]] == ["t-old"]
        assert second["next_offset"] is None

    async def test_empty_sessions_are_skipped_and_the_page_still_fills(self, mem, zep):
        """A reconnect leaves a thread with nothing said; it is not a conversation."""
        zep.user.get_threads.result = [
            thread("t-a", created_at="2026-03-03"),
            thread("t-empty", created_at="2026-03-04"),
            thread("t-b", created_at="2026-03-02"),
        ]
        FakeThreads(zep, {"t-a": [msg("a")], "t-empty": [], "t-b": [msg("b")]})
        body = (await mem.get(self.PATH, params={"limit": 2})).json()
        assert [s["thread_id"] for s in body["sessions"]] == ["t-a", "t-b"]
        assert body["next_offset"] is None

    async def test_empty_sessions_on_request(self, mem, zep):
        zep.user.get_threads.result = [thread("t-empty")]
        FakeThreads(zep, {"t-empty": []})
        body = (await mem.get(self.PATH, params={"include_empty": True})).json()
        assert body["sessions"][0]["empty"] is True
        assert body["sessions"][0]["started_at"] is None
        assert body["sessions"][0]["preview"] is None

    async def test_a_head_without_a_total_counts_what_it_holds(self, mem, zep):
        zep.user.get_threads.result = [thread("t-1")]

        async def get(**kwargs):
            return SimpleNamespace(messages=[msg("a"), msg("b")], user_id=USER_ID)

        zep.thread.get = get
        assert (await mem.get(self.PATH)).json()["sessions"][0]["message_count"] == 2

    async def test_a_session_zep_cannot_describe_is_listed_as_unavailable(self, mem, zep):
        zep.user.get_threads.result = [thread("t-gone")]
        FakeThreads(zep, {})
        body = (await mem.get(self.PATH)).json()
        assert body["sessions"][0]["thread_id"] == "t-gone"
        assert body["sessions"][0]["unavailable"] is True

    async def test_another_tenants_thread_is_never_listed(self, mem, zep):
        zep.user.get_threads.result = [thread("t-x", user_id="someone-else")]
        assert (await mem.get(self.PATH)).json()["sessions"] == []

    async def test_a_user_zep_never_saw_has_no_sessions(self, mem, zep):
        zep.user.get_threads.error = RuntimeError("status_code: 404")
        body = (await mem.get(self.PATH)).json()
        assert body == {"sessions": [], "count": 0, "total_threads": 0, "next_offset": None}

    async def test_a_failing_listing_is_a_502(self, mem, zep):
        zep.user.get_threads.error = RuntimeError("zep down")
        assert (await mem.get(self.PATH)).status_code == 502

    async def test_another_users_sessions_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/sessions")).status_code == 403


class TestSessionMessages:
    async def test_the_transcript_is_in_spoken_order(self, mem, zep):
        two_sessions(zep)
        body = (await mem.get(f"{BASE}/sessions/t-old/messages")).json()
        assert [m["content"] for m in body["messages"]][:2] == [
            "Hola",
            "Quiero  planear\nel viaje a Lisboa",
        ]
        assert body["messages"][0]["name"] == "Jarvis"
        assert body["messages"][0]["role"] == "assistant"
        assert body["total"] == 4
        assert body["has_more"] is False

    async def test_pages_are_one_based(self, mem, zep):
        fake = two_sessions(zep)
        body = (await mem.get(f"{BASE}/sessions/t-old/messages", params={"limit": 3})).json()
        assert body["has_more"] is True
        second = (
            await mem.get(f"{BASE}/sessions/t-old/messages", params={"limit": 3, "page": 2})
        ).json()
        assert [m["content"] for m in second["messages"]] == ["Gracias"]
        assert fake.calls[-1]["cursor"] == 2

    async def test_a_missing_total_is_counted_from_the_page(self, mem, zep):
        async def get(**kwargs):
            return SimpleNamespace(messages=[msg("x")], user_id=USER_ID)

        zep.thread.get = get
        body = (await mem.get(f"{BASE}/sessions/t-1/messages")).json()
        assert body["total"] == 1

    async def test_another_tenants_thread_is_a_404_and_its_messages_stay_hidden(self, mem, zep):
        zep.user.get_threads.result = []
        FakeThreads(zep, {"t-theirs": [msg("secret")]}, owner="someone-else")
        r = await mem.get(f"{BASE}/sessions/t-theirs/messages")
        assert r.status_code == 404
        assert "secret" not in r.text

    async def test_an_unknown_thread_is_a_404(self, mem, zep):
        FakeThreads(zep, {})
        assert (await mem.get(f"{BASE}/sessions/t-none/messages")).status_code == 404

    async def test_a_zep_failure_is_a_502(self, mem, zep):
        zep.thread.get.error = RuntimeError("zep down")
        assert (await mem.get(f"{BASE}/sessions/t-1/messages")).status_code == 502

    async def test_another_users_transcript_is_a_403(self, mem):
        r = await mem.get("/memory/someone-else/sessions/t-1/messages")
        assert r.status_code == 403


class TestSessionSummary:
    async def test_it_returns_zeps_summary(self, mem, zep):
        two_sessions(zep)
        body = (await mem.get(f"{BASE}/sessions/t-old/summary")).json()
        assert body == {
            "thread_id": "t-old",
            "summary": "They talked about the trip.",
            "updated_at": "2026-03-02T00:00:00Z",
        }
        assert zep.thread.get_summary.last == {"_args": ("t-old",)}

    async def test_no_summary_yet_is_null(self, mem, zep):
        two_sessions(zep)
        zep.thread.get_summary.error = RuntimeError("status_code: 404")
        body = (await mem.get(f"{BASE}/sessions/t-old/summary")).json()
        assert body["summary"] is None

    async def test_an_empty_summary_is_null_and_undated(self, mem, zep):
        two_sessions(zep)
        zep.thread.get_summary.result = SimpleNamespace(summary="")
        body = (await mem.get(f"{BASE}/sessions/t-old/summary")).json()
        assert body["summary"] is None
        assert body["updated_at"] is None

    async def test_a_foreign_thread_is_never_summarised(self, mem, zep):
        FakeThreads(zep, {"t-theirs": [msg("x")]}, owner="someone-else")
        assert (await mem.get(f"{BASE}/sessions/t-theirs/summary")).status_code == 404
        assert not zep.thread.get_summary.called

    async def test_a_summary_failure_is_a_502(self, mem, zep):
        two_sessions(zep)
        zep.thread.get_summary.error = RuntimeError("zep down")
        assert (await mem.get(f"{BASE}/sessions/t-old/summary")).status_code == 502


class TestStatus:
    PATH = f"{BASE}/status"

    async def test_a_kwami_with_history(self, mem, zep):
        zep.user.get.result = SimpleNamespace(created_at="2026-02-01T00:00:00Z")
        two_sessions(zep)
        assert (await mem.get(self.PATH)).json() == {
            "configured": True,
            "user_exists": True,
            "created_at": "2026-02-01T00:00:00Z",
            "thread_count": 2,
            "last_session_at": "2026-03-05T09:00:00Z",
        }

    async def test_a_kwami_zep_has_never_seen(self, mem, zep):
        """What an empty panel usually means: no session has ever reached memory."""
        zep.user.get.error = RuntimeError("status_code: 404")
        body = (await mem.get(self.PATH)).json()
        assert body["user_exists"] is False
        assert body["thread_count"] == 0

    async def test_a_user_without_threads(self, mem, zep):
        zep.user.get.result = SimpleNamespace(created_at=None)
        body = (await mem.get(self.PATH)).json()
        assert body["thread_count"] == 0
        assert body["last_session_at"] is None
        assert body["created_at"] is None

    async def test_a_zep_failure_is_a_502(self, mem, zep):
        zep.user.get.error = RuntimeError("zep down")
        assert (await mem.get(self.PATH)).status_code == 502

    async def test_another_users_status_is_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/status")).status_code == 403


class TestThreadHelpers:
    def test_a_long_preview_is_cut_on_a_word(self):
        text = "palabra " * 40
        preview = threads_mod._preview([msg(text)])
        assert len(preview) <= threads_mod.PREVIEW_CHARS
        assert preview.endswith("…")

    def test_the_preview_falls_back_to_the_first_thing_said(self):
        assert threads_mod._preview([msg("Hola", "assistant")]) == "Hola"

    def test_silence_has_no_preview(self):
        assert threads_mod._preview([msg("   ")]) is None

    def test_an_enum_role_is_read_by_value(self):
        role = SimpleNamespace(value="assistant")
        assert threads_mod.role_of(SimpleNamespace(role=role)) == "assistant"

    def test_a_list_answer_is_its_own_messages(self):
        assert threads_mod.messages_of([1, 2]) == [1, 2]
        assert threads_mod.messages_of(None) == []
