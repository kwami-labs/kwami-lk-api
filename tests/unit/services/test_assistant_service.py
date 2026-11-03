"""`src.services.assistant_service`: rules, tasks, settings, notifications, the send record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.services import assistant_service as svc

pytestmark = pytest.mark.anyio

MADRID = "Europe/Madrid"


class TestNextOccurrence:
    def test_a_rule_that_does_not_repeat_has_no_next(self):
        assert svc.next_occurrence("once", [], None, MADRID, datetime.now(UTC)) is None
        assert svc.next_occurrence("none", [], None, MADRID, datetime.now(UTC)) is None

    def test_daily_is_later_today_when_the_hour_has_not_passed(self):
        after = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # 07:00 in Madrid
        assert svc.next_occurrence("daily", [], "08:00", MADRID, after) == datetime(
            2026, 11, 2, 7, 0, tzinfo=UTC
        )

    def test_daily_rolls_to_tomorrow_once_it_has_passed(self):
        after = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # exactly 08:00 in Madrid
        assert svc.next_occurrence("daily", [], "08:00", MADRID, after) == datetime(
            2026, 11, 3, 7, 0, tzinfo=UTC
        )

    def test_weekly_on_the_same_weekday_after_the_hour_is_next_week(self):
        after = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # a Monday
        assert svc.next_occurrence("weekly", [0], "08:00", MADRID, after) == datetime(
            2026, 11, 9, 7, 0, tzinfo=UTC
        )

    def test_it_keeps_the_local_hour_across_a_clock_change(self):
        """Clocks went back on 25 Oct 2026: eight in Madrid moves from 06:00 to 07:00 UTC."""
        before = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        after = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        assert svc.next_occurrence("weekly", [0], "08:00", MADRID, before).hour == 6
        assert svc.next_occurrence("weekly", [0], "08:00", MADRID, after).hour == 7

    @pytest.mark.parametrize("weekdays", [[], [7], [-1]])
    def test_bad_weekdays_are_refused(self, weekdays):
        with pytest.raises(ValueError, match="Weekdays"):
            svc.next_occurrence("weekly", weekdays, "08:00", MADRID, datetime.now(UTC))

    def test_a_bad_time_is_refused(self):
        with pytest.raises(ValueError, match="local_time"):
            svc.next_occurrence("daily", [], "soon", MADRID, datetime.now(UTC))

    def test_a_bad_zone_is_refused(self):
        with pytest.raises(ValueError, match="Unknown timezone"):
            svc.next_occurrence("daily", [], "08:00", "Mars/Olympus", datetime.now(UTC))


class TestParseInstant:
    def test_no_offset_is_read_in_the_users_zone(self):
        parsed = svc.parse_instant("2026-11-02T08:00:00", ZoneInfo(MADRID))
        assert parsed.astimezone(UTC).hour == 7

    def test_zulu_is_utc(self):
        assert svc.parse_instant("2026-11-02T08:00:00Z", ZoneInfo(MADRID)).hour == 8

    def test_garbage_is_refused(self):
        with pytest.raises(ValueError, match="Invalid run_at"):
            svc.parse_instant("mañana", ZoneInfo(MADRID))


class TestSettings:
    async def test_the_defaults_when_nothing_was_saved(self, fake_supabase, tenant):
        row = await svc.get_settings(tenant.user_id, tenant.kwami_id)
        assert row["timezone"] == MADRID
        assert row["reports_enabled"] is False

    async def test_another_tenants_kwami_is_refused(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await svc.get_settings(tenant.user_id, other_tenant.kwami_id)

    async def test_enabling_creates_the_three_reports_on_the_users_days(
        self, fake_supabase, tenant
    ):
        number, code, proof = svc.new_notify_code(tenant.user_id, tenant.kwami_id, "612 345 678")
        row = await svc.save_settings(
            tenant.user_id,
            tenant.kwami_id,
            notify_to="612 345 678",
            notify_channel="whatsapp",
            timezone=MADRID,
            reports_enabled=True,
            notify_code=code,
            notify_proof=proof,
        )
        assert row["notify_to"] == number == "+34612345678"
        reports = {t["report_type"]: t for t in fake_supabase.db.rows("kwami_assistant_tasks")}
        assert set(reports) == {"monday", "friday", "sunday"}
        assert reports["monday"]["weekdays"] == [0]
        assert reports["friday"]["weekdays"] == [4]
        assert reports["sunday"]["weekdays"] == [6]
        assert all(t["next_run_at"] and t["status"] == "open" for t in reports.values())

    async def test_disabling_switches_them_off_without_duplicating(self, fake_supabase, tenant):
        fake_supabase.db.add_unique_index("kwami_assistant_tasks", "kwami_id", "report_type")
        common = {"notify_to": None, "notify_channel": "sms", "timezone": MADRID}
        await svc.save_settings(tenant.user_id, tenant.kwami_id, reports_enabled=True, **common)
        await svc.save_settings(tenant.user_id, tenant.kwami_id, reports_enabled=False, **common)
        rows = fake_supabase.db.rows("kwami_assistant_tasks")
        assert len(rows) == 3
        assert all(t["next_run_at"] is None and t["status"] == "cancelled" for t in rows)

    async def test_a_bad_channel_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="notify_channel"):
            await svc.save_settings(
                tenant.user_id,
                tenant.kwami_id,
                notify_to=None,
                notify_channel="paloma",
                timezone=MADRID,
                reports_enabled=False,
            )

    async def test_a_silent_database_is_an_error(self, fake_supabase, tenant, monkeypatch):
        monkeypatch.setattr(svc, "_single", lambda _result: None)
        with pytest.raises(RuntimeError, match="save"):
            await svc.save_settings(
                tenant.user_id,
                tenant.kwami_id,
                notify_to=None,
                notify_channel="sms",
                timezone=MADRID,
                reports_enabled=False,
            )


class TestTasks:
    def _future(self) -> str:
        return (datetime.now(UTC) + timedelta(days=1)).isoformat()

    async def test_a_one_off_reminder(self, fake_supabase, tenant):
        task = await svc.create_task(
            tenant.user_id,
            tenant.kwami_id,
            kind="reminder",
            title="  Llamar al notario ",
            notes=" antes de las doce ",
            run_at=self._future(),
        )
        assert task["title"] == "Llamar al notario"
        assert task["notes"] == "antes de las doce"
        assert task["recurrence"] == "once"
        assert task["next_run_at"]

    async def test_a_weekly_reminder(self, fake_supabase, tenant):
        task = await svc.create_task(
            tenant.user_id,
            tenant.kwami_id,
            kind="reminder",
            title="Revisar cartera",
            recurrence="weekly",
            weekdays=[2],
            local_time="09:30",
        )
        assert task["recurrence"] == "weekly"
        assert datetime.fromisoformat(task["next_run_at"]).astimezone(ZoneInfo(MADRID)).hour == 9

    async def test_a_followup_needs_no_time(self, fake_supabase, tenant):
        task = await svc.create_task(
            tenant.user_id, tenant.kwami_id, kind="followup", title="Reforma del piso"
        )
        assert task["recurrence"] == "none"
        assert task["next_run_at"] is None

    async def test_a_reminder_with_no_time_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="needs a time"):
            await svc.create_task(tenant.user_id, tenant.kwami_id, kind="reminder", title="X")

    async def test_the_past_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="past"):
            await svc.create_task(
                tenant.user_id,
                tenant.kwami_id,
                kind="reminder",
                title="X",
                run_at="2020-01-01T00:00:00Z",
            )

    async def test_a_report_cannot_be_created_by_hand(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="kind"):
            await svc.create_task(tenant.user_id, tenant.kwami_id, kind="report", title="X")

    async def test_a_blank_title_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Title"):
            await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="  ")

    async def test_an_unknown_rule_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="one-off"):
            await svc.create_task(
                tenant.user_id, tenant.kwami_id, kind="followup", title="X", recurrence="once"
            )

    async def test_a_silent_database_is_an_error(self, fake_supabase, tenant, monkeypatch):
        monkeypatch.setattr(svc, "_single", lambda _result: None)
        with pytest.raises(RuntimeError, match="create"):
            await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="X")

    async def test_listing_filters_by_status(self, fake_supabase, tenant):
        task = await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="A")
        await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="B")
        await svc.update_task(tenant.user_id, task["id"], status="done")
        assert len(await svc.list_tasks(tenant.user_id, tenant.kwami_id)) == 2
        done = await svc.list_tasks(tenant.user_id, tenant.kwami_id, status="done")
        assert [t["title"] for t in done] == ["A"]

    async def test_listing_refuses_an_unknown_status(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Invalid status"):
            await svc.list_tasks(tenant.user_id, tenant.kwami_id, status="maybe")

    async def test_closing_a_task_stops_its_clock(self, fake_supabase, tenant):
        task = await svc.create_task(
            tenant.user_id, tenant.kwami_id, kind="reminder", title="A", run_at=self._future()
        )
        closed = await svc.update_task(tenant.user_id, task["id"], status="cancelled")
        assert closed["next_run_at"] is None

    async def test_updating_title_and_notes(self, fake_supabase, tenant):
        task = await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="A")
        changed = await svc.update_task(tenant.user_id, task["id"], title=" B ", notes=" n ")
        assert (changed["title"], changed["notes"]) == ("B", "n")

    async def test_reopening_keeps_the_rest(self, fake_supabase, tenant):
        task = await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="A")
        assert (await svc.update_task(tenant.user_id, task["id"], status="open"))["title"] == "A"

    async def test_update_refusals(self, fake_supabase, tenant):
        task = await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="A")
        with pytest.raises(ValueError, match="Invalid status"):
            await svc.update_task(tenant.user_id, task["id"], status="maybe")
        with pytest.raises(ValueError, match="Title"):
            await svc.update_task(tenant.user_id, task["id"], title=" ")

    async def test_another_tenants_task_is_not_found(self, fake_supabase, tenant, other_tenant):
        task = await svc.create_task(tenant.user_id, tenant.kwami_id, kind="followup", title="A")
        with pytest.raises(ValueError, match="Task not found"):
            await svc.update_task(other_tenant.user_id, task["id"], title="B")
        assert await svc.delete_task(other_tenant.user_id, task["id"]) is False
        assert await svc.delete_task(tenant.user_id, task["id"]) is True

    async def test_reports_are_only_switched_from_the_settings(self, fake_supabase, tenant):
        await svc.save_settings(
            tenant.user_id,
            tenant.kwami_id,
            notify_to=None,
            notify_channel="sms",
            timezone=MADRID,
            reports_enabled=True,
        )
        report = fake_supabase.db.rows("kwami_assistant_tasks")[0]
        assert await svc.delete_task(tenant.user_id, report["id"]) is False


class TestNotifications:
    def _seed(self, fake_supabase, tenant, **extra):
        return fake_supabase.db.seed(
            "kwami_assistant_notifications",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "title": "T",
                "body": "B",
                "read_at": None,
                **extra,
            },
        )[0]

    async def test_unread_only_and_marking_read(self, fake_supabase, tenant):
        row = self._seed(fake_supabase, tenant)
        unread = await svc.list_notifications(tenant.user_id, tenant.kwami_id, unread_only=True)
        assert len(unread) == 1
        assert await svc.mark_notification_read(tenant.user_id, row["id"]) is True
        assert not await svc.list_notifications(tenant.user_id, tenant.kwami_id, unread_only=True)
        assert len(await svc.list_notifications(tenant.user_id, tenant.kwami_id)) == 1

    async def test_another_tenant_cannot_mark_it(self, fake_supabase, tenant, other_tenant):
        row = self._seed(fake_supabase, tenant)
        assert await svc.mark_notification_read(other_tenant.user_id, row["id"]) is False


class TestExpressSend:
    async def test_it_records_the_users_words(self, fake_supabase, tenant):
        row = await svc.record_express_send(
            tenant.user_id,
            tenant.kwami_id,
            channel="gmail",
            recipient="a@b.c",
            summary="x" * 400,
            express_order="envíalo sin revisar",
        )
        assert row["express_order"] == "envíalo sin revisar"
        assert len(row["summary"]) == 300

    async def test_a_silent_database_is_an_error(self, fake_supabase, tenant, monkeypatch):
        monkeypatch.setattr(svc, "_single", lambda _result: None)
        with pytest.raises(RuntimeError, match="record"):
            await svc.record_express_send(
                tenant.user_id,
                tenant.kwami_id,
                channel="gmail",
                recipient="a@b.c",
                summary="",
                express_order="ya",
            )


class TestScheduledWork:
    async def test_needs_a_time_and_an_instruction(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="needs a time"):
            await svc.create_task(
                tenant.user_id, tenant.kwami_id, kind="work", title="X", notes="Haz X"
            )
        with pytest.raises(ValueError, match="instruction"):
            await svc.create_task(
                tenant.user_id,
                tenant.kwami_id,
                kind="work",
                title="X",
                recurrence="daily",
                local_time="08:00",
            )

    async def test_created_with_its_schedule(self, fake_supabase, tenant):
        row = await svc.create_task(
            tenant.user_id,
            tenant.kwami_id,
            kind="work",
            title="Revisar mercados",
            notes="Resume mis posiciones.",
            recurrence="daily",
            local_time="08:00",
        )
        assert row["kind"] == "work"
        assert row["next_run_at"]


class TestConfirmingANumber:
    COMMON = {"notify_channel": "sms", "timezone": MADRID, "reports_enabled": False}

    async def _save(self, tenant, number="612345678", **proof):
        return await svc.save_settings(
            tenant.user_id, tenant.kwami_id, notify_to=number, **self.COMMON, **proof
        )

    async def test_a_new_number_needs_its_code(self, fake_supabase, tenant):
        with pytest.raises(svc.NotifyNumberNotConfirmedError):
            await self._save(tenant)

    async def test_the_wrong_code_is_refused(self, fake_supabase, tenant):
        _, _, proof = svc.new_notify_code(tenant.user_id, tenant.kwami_id, "612345678")
        with pytest.raises(svc.NotifyNumberNotConfirmedError):
            await self._save(tenant, notify_code="not-it", notify_proof=proof)

    async def test_a_code_is_for_the_number_it_was_sent_to(self, fake_supabase, tenant):
        _, code, proof = svc.new_notify_code(tenant.user_id, tenant.kwami_id, "699999999")
        with pytest.raises(svc.NotifyNumberNotConfirmedError):
            await self._save(tenant, notify_code=code, notify_proof=proof)

    async def test_an_expired_code_is_refused(self, fake_supabase, tenant, monkeypatch):
        monkeypatch.setattr(svc, "NOTIFY_CODE_SECONDS", -1)
        _, code, proof = svc.new_notify_code(tenant.user_id, tenant.kwami_id, "612345678")
        with pytest.raises(svc.NotifyNumberNotConfirmedError):
            await self._save(tenant, notify_code=code, notify_proof=proof)

    async def test_a_proof_that_is_not_one_is_refused(self, fake_supabase, tenant):
        with pytest.raises(svc.NotifyNumberNotConfirmedError):
            await self._save(tenant, notify_code="123456", notify_proof="nonsense")

    async def test_a_number_already_saved_needs_nothing(self, fake_supabase, tenant):
        _, code, proof = svc.new_notify_code(tenant.user_id, tenant.kwami_id, "612345678")
        await self._save(tenant, notify_code=code, notify_proof=proof)
        assert (await self._save(tenant))["notify_to"] == "+34612345678"

    async def test_removing_the_number_needs_nothing(self, fake_supabase, tenant):
        row = await self._save(tenant, number=None)
        assert row["notify_to"] is None
