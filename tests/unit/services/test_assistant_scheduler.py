"""`src.services.assistant_scheduler`: who fires, what reports say, how they arrive."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.services import assistant_scheduler as sched
from src.services import google_service

pytestmark = pytest.mark.anyio

MADRID = "Europe/Madrid"
# A Monday, 08:00 in Madrid.
MONDAY = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


def _task(fake_supabase, tenant, **extra) -> dict:
    return fake_supabase.db.seed(
        "kwami_assistant_tasks",
        {
            "user_id": tenant.user_id,
            "kwami_id": tenant.kwami_id,
            "kind": "reminder",
            "title": "Llamar al notario",
            "notes": "",
            "recurrence": "once",
            "weekdays": [],
            "local_time": None,
            "timezone": MADRID,
            "report_type": None,
            "next_run_at": (MONDAY - timedelta(minutes=1)).isoformat(),
            "status": "open",
            **extra,
        },
    )[0]


def _settings(fake_supabase, tenant, **extra) -> None:
    fake_supabase.db.seed(
        "kwami_assistant_settings",
        {
            "kwami_id": tenant.kwami_id,
            "user_id": tenant.user_id,
            "notify_to": "+34612345678",
            "notify_channel": "whatsapp",
            "timezone": MADRID,
            "reports_enabled": True,
            **extra,
        },
    )


def _channel(fake_supabase, tenant, kind: str, **extra) -> None:
    fake_supabase.db.seed(
        "kwami_channels",
        {
            "user_id": tenant.user_id,
            "kwami_id": tenant.kwami_id,
            "kind": kind,
            "phone_number": "+34900000000",
            "provider_sender": None,
            "status": "active",
            **extra,
        },
    )


@pytest.fixture
def sent(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def whatsapp(*, from_address, to_address, body):
        calls.append(("whatsapp", from_address, to_address, body))

    async def sms(*, from_number, to_number, body):
        calls.append(("sms", from_number, to_number, body))

    monkeypatch.setattr(sched, "send_whatsapp_message", whatsapp)
    monkeypatch.setattr(sched, "send_sms_message", sms)
    return calls


def _no_google(monkeypatch) -> None:
    monkeypatch.setattr(sched.settings, "google_oauth_client_id", None, raising=False)


class TestTick:
    async def test_a_due_reminder_fires_once_and_finishes(self, fake_supabase, tenant, sent):
        task = _task(fake_supabase, tenant)
        assert await sched.tick(MONDAY) == 1
        row = fake_supabase.db.rows("kwami_assistant_tasks")[0]
        assert row["status"] == "done"
        assert row["next_run_at"] is None
        note = fake_supabase.db.rows("kwami_assistant_notifications")[0]
        assert note["title"] == "Llamar al notario"
        assert note["task_id"] == task["id"]
        assert await sched.tick(MONDAY) == 0

    async def test_what_is_not_due_stays(self, fake_supabase, tenant, sent):
        _task(fake_supabase, tenant, next_run_at=(MONDAY + timedelta(hours=1)).isoformat())
        assert await sched.tick(MONDAY) == 0

    async def test_a_repeating_reminder_moves_to_its_next_time(self, fake_supabase, tenant, sent):
        _task(fake_supabase, tenant, recurrence="daily", local_time="08:30")
        await sched.tick(MONDAY)
        row = fake_supabase.db.rows("kwami_assistant_tasks")[0]
        assert row["status"] == "open"
        assert row["next_run_at"].startswith("2026-11-02T07:30")

    async def test_a_task_someone_else_claimed_does_not_fire(self, fake_supabase, tenant, sent):
        task = _task(fake_supabase, tenant)
        stale = {**task, "next_run_at": "2020-01-01T00:00:00+00:00"}
        assert await sched.fire(stale, MONDAY) is False
        assert not fake_supabase.db.rows("kwami_assistant_notifications")

    async def test_one_failure_does_not_stop_the_rest(
        self, fake_supabase, tenant, sent, monkeypatch, caplog
    ):
        _task(fake_supabase, tenant, title="A")
        _task(fake_supabase, tenant, title="B")
        real = sched.deliver

        async def flaky(user_id, kwami_id, task_id, title, body):
            if title == "A":
                raise RuntimeError("boom")
            return await real(user_id, kwami_id, task_id, title, body)

        monkeypatch.setattr(sched, "deliver", flaky)
        assert await sched.tick(MONDAY) == 1
        assert "failed" in caplog.text

    async def test_it_uses_the_clock_when_given_no_time(self, fake_supabase, tenant, sent):
        _task(fake_supabase, tenant, next_run_at="2020-01-01T00:00:00+00:00")
        assert await sched.tick() == 1

    async def test_a_stale_report_is_skipped(self, fake_supabase, tenant, sent):
        _task(
            fake_supabase,
            tenant,
            kind="report",
            report_type="monday",
            recurrence="weekly",
            weekdays=[0],
            local_time="08:00",
            next_run_at=(MONDAY - timedelta(days=2)).isoformat(),
        )
        assert await sched.tick(MONDAY) == 0
        assert not fake_supabase.db.rows("kwami_assistant_notifications")
        assert fake_supabase.db.rows("kwami_assistant_tasks")[0]["next_run_at"] > MONDAY.isoformat()

    async def test_a_due_report_is_written_and_delivered(
        self, fake_supabase, tenant, sent, monkeypatch
    ):
        _no_google(monkeypatch)
        _settings(fake_supabase, tenant)
        _channel(fake_supabase, tenant, "whatsapp", provider_sender="+34911111111")
        _task(
            fake_supabase,
            tenant,
            kind="report",
            report_type="monday",
            recurrence="weekly",
            weekdays=[0],
            local_time="08:00",
        )
        assert await sched.tick(MONDAY) == 1
        assert sent[0][:3] == ("whatsapp", "whatsapp:+34911111111", "whatsapp:+34612345678")
        assert sent[0][3].startswith("Informe del lunes")


class TestReports:
    def _event(self, fake_supabase, tenant, title: str, when: datetime) -> None:
        fake_supabase.db.seed(
            "kwami_calendar_events",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "title": title,
                "starts_at": when.isoformat(),
                "ends_at": (when + timedelta(hours=1)).isoformat(),
            },
        )

    async def test_monday_is_this_week(self, fake_supabase, tenant, monkeypatch):
        _no_google(monkeypatch)
        self._event(fake_supabase, tenant, "Dentista", MONDAY + timedelta(days=2))
        self._event(fake_supabase, tenant, "Fuera", MONDAY + timedelta(days=9))
        _task(
            fake_supabase,
            tenant,
            title="Pagar IVA",
            next_run_at=(MONDAY + timedelta(days=1)).isoformat(),
        )
        _task(
            fake_supabase,
            tenant,
            kind="followup",
            title="Reforma",
            notes="presupuesto",
            next_run_at=None,
            recurrence="none",
        )
        title, body = await sched.build_report(
            "monday", tenant.user_id, tenant.kwami_id, MADRID, MONDAY
        )
        assert title == "Informe del lunes"
        assert "- mié 04/11 08:00: Dentista" in body
        assert "Fuera" not in body
        assert "- mar 03/11 08:00: Pagar IVA" in body
        assert "- Reforma (presupuesto)" in body

    async def test_an_empty_week_says_so(self, fake_supabase, tenant, monkeypatch):
        _no_google(monkeypatch)
        _, body = await sched.build_report(
            "monday", tenant.user_id, tenant.kwami_id, MADRID, MONDAY
        )
        assert "Sin citas." in body
        assert "Ninguno." in body
        assert "Nada abierto." in body

    async def test_sunday_is_the_week_after(self, fake_supabase, tenant, monkeypatch):
        _no_google(monkeypatch)
        sunday = MONDAY + timedelta(days=6, hours=12)
        self._event(fake_supabase, tenant, "Esta semana", MONDAY + timedelta(days=2))
        self._event(fake_supabase, tenant, "Próxima", MONDAY + timedelta(days=8))
        title, body = await sched.build_report(
            "sunday", tenant.user_id, tenant.kwami_id, MADRID, sunday
        )
        assert title == "Recordatorio: la semana que viene"
        assert "Próxima" in body
        assert "Esta semana" not in body

    async def test_friday_closes_the_week(self, fake_supabase, tenant, monkeypatch):
        _no_google(monkeypatch)
        friday = MONDAY + timedelta(days=4, hours=10)
        _task(
            fake_supabase,
            tenant,
            kind="followup",
            title="Hecho",
            status="done",
            next_run_at=None,
            updated_at=(MONDAY + timedelta(days=1)).isoformat(),
        )
        _task(
            fake_supabase,
            tenant,
            kind="followup",
            title="Antiguo",
            status="done",
            next_run_at=None,
            updated_at=(MONDAY - timedelta(days=9)).isoformat(),
        )
        _task(fake_supabase, tenant, kind="followup", title="Abierto", next_run_at=None)
        self._event(fake_supabase, tenant, "Próxima", MONDAY + timedelta(days=8))
        title, body = await sched.build_report(
            "friday", tenant.user_id, tenant.kwami_id, MADRID, friday
        )
        assert title == "Informe del viernes"
        assert "- Hecho" in body
        assert "Antiguo" not in body
        assert "- Abierto" in body
        assert "Próxima" in body

    async def test_google_events_come_in_order_with_all_day_ones(
        self, fake_supabase, tenant, monkeypatch
    ):
        monkeypatch.setattr(type(sched.settings), "google_enabled", property(lambda _s: True))

        async def events(_user, _start, _end):
            return [
                {"starts_at": "2026-11-03", "title": "Festivo"},
                {"starts_at": "2026-11-05T10:00:00Z", "title": "Reunión"},
            ]

        monkeypatch.setattr(sched.google_service, "list_events", events)
        self._event(fake_supabase, tenant, "Dentista", MONDAY + timedelta(days=2))
        _, body = await sched.build_report(
            "monday", tenant.user_id, tenant.kwami_id, MADRID, MONDAY
        )
        assert body.index("Festivo") < body.index("Dentista") < body.index("Reunión")
        assert "- mar 03/11: Festivo" in body

    async def test_an_unlinked_google_account_is_not_a_problem(
        self, fake_supabase, tenant, monkeypatch
    ):
        monkeypatch.setattr(type(sched.settings), "google_enabled", property(lambda _s: True))

        async def events(*_args):
            raise google_service.GoogleNotConnectedError("no")

        monkeypatch.setattr(sched.google_service, "list_events", events)
        _, body = await sched.build_report(
            "monday", tenant.user_id, tenant.kwami_id, MADRID, MONDAY
        )
        assert "Google" not in body

    async def test_a_google_failure_is_told_not_hidden(self, fake_supabase, tenant, monkeypatch):
        monkeypatch.setattr(type(sched.settings), "google_enabled", property(lambda _s: True))

        async def events(*_args):
            raise google_service.GoogleRequestError("no")

        monkeypatch.setattr(sched.google_service, "list_events", events)
        _, body = await sched.build_report(
            "monday", tenant.user_id, tenant.kwami_id, MADRID, MONDAY
        )
        assert "No he podido leer su Google Calendar." in body


class TestSendNotifyCode:
    async def test_the_code_goes_to_the_number_and_the_proof_back(
        self, fake_supabase, tenant, sent
    ):
        _channel(fake_supabase, tenant, "sms")
        proof = await sched.send_notify_code(tenant.user_id, tenant.kwami_id, "612345678", "sms")
        assert "." in proof
        assert sent and "Kwami" in str(sent[0])

    async def test_no_way_to_send_it_is_a_failure(self, fake_supabase, tenant, sent):
        with pytest.raises(RuntimeError, match="could not be sent"):
            await sched.send_notify_code(tenant.user_id, tenant.kwami_id, "612345678", "whatsapp")


class TestDeliver:
    async def test_with_no_phone_it_stays_in_the_app(self, fake_supabase, tenant, sent):
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "B")
        assert note["delivered_via"] == "app"
        assert note["delivery_error"] is None
        assert sent == []

    async def test_whatsapp_prefers_the_channels_sender(self, fake_supabase, tenant, sent):
        _settings(fake_supabase, tenant)
        _channel(fake_supabase, tenant, "whatsapp", provider_sender="whatsapp:+34911111111")
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "B")
        assert note["delivered_via"] == "whatsapp"
        assert sent[0][1] == "whatsapp:+34911111111"

    async def test_sms_when_it_is_the_choice(self, fake_supabase, tenant, sent):
        _settings(fake_supabase, tenant, notify_channel="sms")
        _channel(fake_supabase, tenant, "sms")
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "B")
        assert note["delivered_via"] == "sms"
        assert sent[0][:3] == ("sms", "+34900000000", "+34612345678")

    async def test_a_whatsapp_failure_falls_through_to_sms(
        self, fake_supabase, tenant, sent, monkeypatch
    ):
        _settings(fake_supabase, tenant)
        _channel(fake_supabase, tenant, "whatsapp", provider_sender="+34911111111")
        _channel(fake_supabase, tenant, "sms")

        async def refused(**_kwargs):
            raise RuntimeError("outside the 24-hour window")

        monkeypatch.setattr(sched, "send_whatsapp_message", refused)
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "B")
        assert note["delivered_via"] == "sms"
        assert note["delivery_error"] is None

    async def test_when_nothing_works_it_is_on_record(
        self, fake_supabase, tenant, sent, monkeypatch
    ):
        monkeypatch.setattr(sched.settings, "twilio_whatsapp_from", None, raising=False)
        _settings(fake_supabase, tenant)
        _channel(fake_supabase, tenant, "whatsapp")
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "B")
        assert note["delivered_via"] == "app"
        assert "whatsapp: RuntimeError" in note["delivery_error"]
        assert "sms: RuntimeError" in note["delivery_error"]

    async def test_a_long_report_is_clipped_for_the_phone_only(self, fake_supabase, tenant, sent):
        _settings(fake_supabase, tenant, notify_channel="sms")
        _channel(fake_supabase, tenant, "sms")
        note = await sched.deliver(tenant.user_id, tenant.kwami_id, None, "T", "x" * 3000)
        assert len(sent[0][3]) == sched.MAX_MESSAGE_CHARS
        assert sent[0][3].endswith(sched.MORE_IN_APP)
        assert len(note["body"]) == 3000


class TestLifecycle:
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(sched.settings, "supabase_url", "http://db", raising=False)
        monkeypatch.setattr(sched.settings, "supabase_secret_key", "k", raising=False)
        monkeypatch.setattr(sched.settings, "assistant_scheduler_enabled", True, raising=False)

    async def test_it_runs_and_stops(self, monkeypatch):
        ticked = asyncio.Event()

        async def tick():
            ticked.set()

        monkeypatch.setattr(sched, "tick", tick)
        assert sched.start_scheduler() is True
        await asyncio.wait_for(ticked.wait(), timeout=1)
        await sched.stop_scheduler()
        assert sched._scheduler_task is None

    async def test_a_failed_tick_does_not_kill_the_loop(self, monkeypatch, caplog):
        monkeypatch.setattr(
            sched.settings, "assistant_scheduler_interval_seconds", 0, raising=False
        )
        calls = 0
        again = asyncio.Event()

        async def tick():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            again.set()

        monkeypatch.setattr(sched, "tick", tick)
        sched.start_scheduler()
        await asyncio.wait_for(again.wait(), timeout=1)
        await sched.stop_scheduler()
        assert "tick failed" in caplog.text

    async def test_disabled(self, monkeypatch):
        monkeypatch.setattr(sched.settings, "assistant_scheduler_enabled", False, raising=False)
        assert sched.start_scheduler() is False

    async def test_no_database(self, monkeypatch):
        monkeypatch.setattr(sched.settings, "supabase_url", None, raising=False)
        assert sched.start_scheduler() is False

    async def test_stopping_what_never_started_is_fine(self):
        await sched.stop_scheduler()


class TestScheduledWork:
    async def test_firing_queues_the_job_and_sends_nothing(self, fake_supabase, tenant):
        task = _task(
            fake_supabase,
            tenant,
            kind="work",
            title="Revisar mercados",
            notes="Resume cómo han abierto mis posiciones.",
            recurrence="daily",
            local_time="08:00",
        )

        assert await sched.tick(MONDAY) == 1

        jobs = fake_supabase.db.rows("brain_jobs")
        assert len(jobs) == 1
        assert jobs[0]["source"] == "schedule"
        assert jobs[0]["schedule_task_id"] == task["id"]
        assert jobs[0]["task"] == "Resume cómo han abierto mis posiciones."
        assert fake_supabase.db.rows("kwami_assistant_notifications") == []

    async def test_the_agent_and_where_it_runs_go_with_the_job(self, fake_supabase, tenant):
        _task(
            fake_supabase,
            tenant,
            kind="work",
            title="Facturas",
            notes="Revisa las facturas.",
            recurrence="daily",
            local_time="08:00",
            agent_id="agent-1",
            target="cloud",
        )
        _task(
            fake_supabase,
            tenant,
            kind="work",
            title="Sin agente",
            notes="Haz algo.",
            recurrence="daily",
            local_time="08:00",
            target="auto",
        )

        assert await sched.tick(MONDAY) == 2

        jobs = {j["title"]: j for j in fake_supabase.db.rows("brain_jobs")}
        assert (jobs["Facturas"]["agent_id"], jobs["Facturas"]["target"]) == ("agent-1", "cloud")
        # The defaults are left to the database, so it queues before migration 029 too.
        assert "agent_id" not in jobs["Sin agente"] and "target" not in jobs["Sin agente"]

    async def test_work_asked_from_the_app_is_notified_too(self, fake_supabase, tenant):
        self._job(fake_supabase, tenant, source="app")
        assert await sched.notify_finished_work() == 1

    def _job(self, fake_supabase, tenant, **extra) -> dict:
        return fake_supabase.db.seed(
            "brain_jobs",
            {
                "id": str(uuid4()),
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "source": "schedule",
                "schedule_task_id": None,
                "title": "Revisar mercados",
                "status": "done",
                "result": "Todo en verde.",
                "finished_at": MONDAY.isoformat(),
                "notified_at": None,
                **extra,
            },
        )[0]

    async def test_a_finished_job_is_notified_once(self, fake_supabase, tenant):
        self._job(fake_supabase, tenant)

        assert await sched.notify_finished_work() == 1
        assert await sched.notify_finished_work() == 0

        notices = fake_supabase.db.rows("kwami_assistant_notifications")
        assert [(n["title"], n["body"]) for n in notices] == [
            ("Revisar mercados", "Todo en verde.")
        ]

    async def test_a_failed_job_says_it_did_not_finish(self, fake_supabase, tenant):
        self._job(fake_supabase, tenant, status="failed", result="")

        await sched.notify_finished_work()

        notice = fake_supabase.db.rows("kwami_assistant_notifications")[0]
        assert notice["title"] == "No terminado: Revisar mercados"
        assert notice["body"] == "Sin resultado."

    async def test_running_and_spoken_jobs_are_left_alone(self, fake_supabase, tenant):
        self._job(fake_supabase, tenant, status="running", finished_at=None)
        self._job(fake_supabase, tenant, source="call")

        assert await sched.notify_finished_work() == 0

    async def test_a_notice_another_machine_claimed_is_not_sent_twice(self, monkeypatch):
        class Query:
            results = [
                [
                    {
                        "id": "j1",
                        "user_id": "u",
                        "kwami_id": "k",
                        "title": "T",
                        "status": "done",
                        "result": "R",
                    }
                ],
                [],
            ]

            def __getattr__(self, name):
                return lambda *args, **kwargs: self

            async def execute(self):
                return SimpleNamespace(data=Query.results.pop(0))

        async def never(*args):
            raise AssertionError("sent twice")

        monkeypatch.setattr(sched, "get_supabase_admin", lambda: Query())
        monkeypatch.setattr(sched, "deliver", never)

        assert await sched.notify_finished_work() == 0

    async def test_one_failed_notice_does_not_hold_up_the_rest(
        self, fake_supabase, tenant, monkeypatch, caplog
    ):
        first = self._job(fake_supabase, tenant, title="Uno")
        self._job(fake_supabase, tenant, title="Dos")
        real = sched.deliver

        async def flaky(user_id, kwami_id, task_id, title, body):
            if title == first["title"]:
                raise RuntimeError("down")
            return await real(user_id, kwami_id, task_id, title, body)

        monkeypatch.setattr(sched, "deliver", flaky)

        assert await sched.notify_finished_work() == 1
        assert "Could not notify the end of job" in caplog.text

    async def test_tick_survives_an_unreadable_queue(self, fake_supabase, monkeypatch, caplog):
        async def broken() -> int:
            raise RuntimeError("no table yet")

        monkeypatch.setattr(sched, "notify_finished_work", broken)

        assert await sched.tick(MONDAY) == 0
        assert "Could not read finished scheduled work" in caplog.text
