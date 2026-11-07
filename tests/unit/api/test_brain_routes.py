"""`/brain`: work asked from the app, the cloud's switch, the user's agents and sync.

Who takes a job is decided in SQL and pinned against Postgres in
`tests/integration/db/test_brain_cloud.py`; what is pinned here is what the app
can ask for, what it is told, and that nobody reaches another user's rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from postgrest.exceptions import APIError

from src.services import brain_service
from src.services.credits import get_supabase_admin

pytestmark = pytest.mark.anyio

AGENT = {
    "name": "facturas",
    "description": "Revisa las facturas del mes",
    "prompt": "Lee las facturas y resume lo que falta por pagar.",
}


def _now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + offset).isoformat()


async def _seed(table: str, row: dict[str, Any]) -> None:
    await get_supabase_admin().table(table).insert(row).execute()


async def _pc(user_id: str, *, seen: str | None = None) -> None:
    await _seed(
        "brain_runners",
        {
            "user_id": user_id,
            "runner_id": "r1",
            "claude_login": True,
            "last_seen_at": seen or _now(),
        },
    )


async def _cloud(*, seen: str | None = None) -> None:
    await _seed("brain_cloud_runners", {"runner_id": "cloud-1", "last_seen_at": seen or _now()})


async def _rows(table: str) -> list[dict[str, Any]]:
    return (await get_supabase_admin().table(table).select("*").execute()).data


class _NotMigrated:
    """A database without migration 029: every query fails as PostgREST does."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: self

    async def execute(self) -> Any:
        raise APIError({"message": "relation does not exist", "code": "42P01"})


class _Broken(_NotMigrated):
    async def execute(self) -> Any:
        raise APIError({"message": "boom", "code": "XX000"})


@pytest.mark.parametrize(
    ("target", "pc", "cloud", "allowed", "where"),
    [
        ("auto", True, True, True, "pc"),
        ("auto", False, True, True, "cloud"),
        ("auto", False, True, False, "waiting"),
        ("auto", False, False, True, "waiting"),
        ("pc", False, True, True, "waiting"),
        ("pc", True, True, True, "pc"),
        ("cloud", True, True, True, "cloud"),
        ("cloud", True, False, True, "waiting"),
        ("cloud", True, True, False, "waiting"),
    ],
)
def test_placement_follows_the_claim_functions(target, pc, cloud, allowed, where) -> None:
    assert brain_service.placement(target, pc, cloud, allowed) == where


@pytest.mark.parametrize(
    ("task", "title", "expected"),
    [
        ("Busca vuelos\ny compáralos", "", "Busca vuelos"),
        ("x", "  Mi título  ", "Mi título"),
        ("   ", "", "Encargo"),
        ("a" * 300, "", "a" * 200),
    ],
)
def test_a_job_gets_a_short_title(task, title, expected) -> None:
    assert brain_service._title(task, title) == expected


class TestWhere:
    async def test_nothing_there(self, tenant_client):
        r = await tenant_client.get("/brain/where")
        assert r.status_code == 200
        assert r.json() == {
            "pc": {"online": False, "claude_login": False},
            "cloud": {"online": False, "enabled": False},
            "sync": {"pending": 0},
            "auto": "waiting",
        }

    async def test_pc_cloud_and_sync(self, tenant_client, tenant):
        await _pc(tenant.user_id)
        await _cloud()
        await tenant_client.put("/brain/cloud", json={"enabled": True})
        await _seed(
            "brain_sync",
            {"id": "s1", "user_id": tenant.user_id, "status": "pending", "storage_prefix": "p"},
        )
        body = (await tenant_client.get("/brain/where")).json()
        assert body["pc"] == {"online": True, "claude_login": True}
        assert body["cloud"] == {"online": True, "enabled": True}
        assert body["sync"] == {"pending": 1}
        assert body["auto"] == "pc"

    async def test_stale_workers_are_gone(self, tenant_client, tenant):
        await _pc(tenant.user_id, seen=_now(-timedelta(minutes=5)))
        await _cloud(seen=_now(-timedelta(minutes=5)))
        await tenant_client.put("/brain/cloud", json={"enabled": True})
        body = (await tenant_client.get("/brain/where")).json()
        assert body["pc"]["online"] is False and body["cloud"]["online"] is False

    async def test_the_cloud_switch_is_saved_and_changed(self, tenant_client, tenant):
        assert (await tenant_client.put("/brain/cloud", json={"enabled": True})).json() == {
            "enabled": True
        }
        await tenant_client.put("/brain/cloud", json={"enabled": False})
        rows = await _rows("brain_cloud_settings")
        assert [(r["user_id"], r["enabled"]) for r in rows] == [(tenant.user_id, False)]


class TestJobs:
    async def test_work_from_the_phone_goes_to_the_cloud_with_the_pc_off(
        self, tenant_client, tenant
    ):
        await _cloud()
        await tenant_client.put("/brain/cloud", json={"enabled": True})
        r = await tenant_client.post(
            "/brain/jobs", json={"kwami_id": tenant.kwami_id, "task": "Resume mi semana"}
        )
        assert r.status_code == 200
        assert r.json()["where"] == "cloud" and r.json()["target"] == "auto"
        row = (await _rows("brain_jobs"))[0]
        assert row["source"] == "app" and row["title"] == "Resume mi semana"
        assert row["user_id"] == tenant.user_id and "agent_id" not in row

    async def test_work_for_the_pc_waits_for_it(self, tenant_client, tenant):
        await _cloud()
        await tenant_client.put("/brain/cloud", json={"enabled": True})
        r = await tenant_client.post(
            "/brain/jobs",
            json={"kwami_id": tenant.kwami_id, "task": "Ordena Descargas", "target": "pc"},
        )
        assert r.json()["where"] == "waiting"

    async def test_only_on_your_own_kwami(self, tenant_client, other_tenant):
        r = await tenant_client.post(
            "/brain/jobs", json={"kwami_id": other_tenant.kwami_id, "task": "x"}
        )
        assert r.status_code == 400
        assert await _rows("brain_jobs") == []

    async def test_an_empty_or_huge_task_is_refused(self, tenant_client, tenant):
        for task in ("", "x" * (brain_service.MAX_TASK_CHARS + 1)):
            r = await tenant_client.post(
                "/brain/jobs", json={"kwami_id": tenant.kwami_id, "task": task}
            )
            assert r.status_code == 422

    async def test_too_much_waiting_is_a_429(self, tenant_client, tenant, monkeypatch):
        monkeypatch.setattr(brain_service, "MAX_QUEUED", 1)
        body = {"kwami_id": tenant.kwami_id, "task": "x"}
        assert (await tenant_client.post("/brain/jobs", json=body)).status_code == 200
        assert (await tenant_client.post("/brain/jobs", json=body)).status_code == 429

    async def test_the_list_says_where_each_ran_and_hides_the_task(self, tenant_client, tenant):
        for runner, created in (("cloud-a", 1), ("pc-host", 2), ("", 3)):
            await _seed(
                "brain_jobs",
                {
                    "id": f"j{created}",
                    "user_id": tenant.user_id,
                    "kwami_id": tenant.kwami_id,
                    "mode": "work",
                    "title": f"t{created}",
                    "task": "secret profile",
                    "runner_id": runner,
                    "created_at": f"2026-11-0{created}T00:00:00+00:00",
                },
            )
        await _seed(
            "brain_jobs",
            {"id": "think", "user_id": tenant.user_id, "mode": "think", "created_at": "2026"},
        )
        r = await tenant_client.get(f"/brain/jobs?kwami_id={tenant.kwami_id}")
        jobs = r.json()["jobs"]
        assert [(j["id"], j["ran_on"]) for j in jobs] == [
            ("j3", ""),
            ("j2", "pc"),
            ("j1", "cloud"),
        ]
        assert all("task" not in j and "runner_id" not in j for j in jobs)
        assert len((await tenant_client.get("/brain/jobs?limit=1")).json()["jobs"]) == 1

    async def test_nobody_sees_another_users_jobs(self, tenant_client, other_tenant):
        await _seed(
            "brain_jobs",
            {"id": "theirs", "user_id": other_tenant.user_id, "mode": "work", "created_at": "x"},
        )
        assert (await tenant_client.get("/brain/jobs")).json()["jobs"] == []

    async def test_a_queued_job_is_cancelled_and_a_running_one_asked_to_stop(
        self, tenant_client, tenant
    ):
        queued_id = "00000000-0000-4000-8000-000000000001"
        running_id = "00000000-0000-4000-8000-000000000002"
        done_id = "00000000-0000-4000-8000-000000000004"
        for job_id, status in ((queued_id, "queued"), (running_id, "running"), (done_id, "done")):
            await _seed("brain_jobs", {"id": job_id, "user_id": tenant.user_id, "status": status})

        assert (await tenant_client.post(f"/brain/jobs/{done_id}/cancel")).status_code == 404
        assert (await tenant_client.post(f"/brain/jobs/{queued_id}/cancel")).json() == {
            "cancelled": True
        }
        assert (await tenant_client.post(f"/brain/jobs/{running_id}/cancel")).status_code == 200
        rows = {r["id"]: r for r in await _rows("brain_jobs")}
        assert rows[queued_id]["status"] == "cancelled"
        assert rows[running_id]["cancel_requested"] is True
        missing = "00000000-0000-4000-8000-000000000009"
        assert (await tenant_client.post(f"/brain/jobs/{missing}/cancel")).status_code == 404

    async def test_someone_elses_job_cannot_be_cancelled(self, tenant_client, other_tenant):
        job_id = "00000000-0000-4000-8000-000000000003"
        await _seed(
            "brain_jobs", {"id": job_id, "user_id": other_tenant.user_id, "status": "queued"}
        )
        assert (await tenant_client.post(f"/brain/jobs/{job_id}/cancel")).status_code == 404
        assert (await _rows("brain_jobs"))[0]["status"] == "queued"


class TestAgents:
    async def test_create_list_update_and_delete(self, tenant_client, tenant):
        r = await tenant_client.post("/brain/agents", json={**AGENT, "tools": ["Read", "Read"]})
        assert r.status_code == 201
        agent = r.json()
        assert agent["model"] == "claude-sonnet-5-5" and agent["tools"] == ["Read"]
        assert agent["user_id"] == tenant.user_id and agent["target"] == "auto"

        listed = (await tenant_client.get("/brain/agents")).json()
        assert [a["name"] for a in listed["agents"]] == ["facturas"]
        assert "claude-opus-5-5" in listed["models"] and "Bash" in listed["tools"]

        r = await tenant_client.put(
            f"/brain/agents/{agent['id']}",
            json={
                "target": "cloud",
                "model": "claude-haiku-5-5",
                "enabled": False,
                "effort": "high",
            },
        )
        assert r.status_code == 200
        assert (r.json()["target"], r.json()["model"], r.json()["enabled"]) == (
            "cloud",
            "claude-haiku-5-5",
            False,
        )
        unchanged = await tenant_client.put(f"/brain/agents/{agent['id']}", json={})
        assert unchanged.json()["target"] == "cloud"

        assert (await tenant_client.delete(f"/brain/agents/{agent['id']}")).status_code == 204
        assert (await tenant_client.get("/brain/agents")).json()["agents"] == []

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"name": "Con Espacios"}, "name"),
            ({"description": ""}, "description"),
            ({"prompt": "x" * 8001}, "prompt"),
            ({"model": "gpt-4"}, "model"),
            ({"tools": ["Read", "DeleteEverything"]}, "tools"),
            ({"effort": "max"}, "effort"),
            ({"target": "moon"}, "target"),
        ],
    )
    async def test_bad_fields_are_refused(self, tenant_client, change, message):
        r = await tenant_client.post("/brain/agents", json={**AGENT, **change})
        assert r.status_code == 400
        assert message in r.json()["detail"]

    async def test_the_basics_are_needed_and_names_are_unique(self, tenant_client):
        r = await tenant_client.post("/brain/agents", json={"name": "solo"})
        assert r.status_code == 400 and "needed" in r.json()["detail"]
        assert (await tenant_client.post("/brain/agents", json=AGENT)).status_code == 201
        r = await tenant_client.post("/brain/agents", json=AGENT)
        assert r.status_code == 400 and "in use" in r.json()["detail"]

    async def test_renaming_onto_another_agent_is_refused(self, tenant_client):
        await tenant_client.post("/brain/agents", json=AGENT)
        other = (await tenant_client.post("/brain/agents", json={**AGENT, "name": "otro"})).json()
        r = await tenant_client.put(f"/brain/agents/{other['id']}", json={"name": "facturas"})
        assert r.status_code == 400
        r = await tenant_client.put(f"/brain/agents/{other['id']}", json={"name": "nuevo"})
        assert r.json()["name"] == "nuevo"

    async def test_there_is_a_limit(self, tenant_client, monkeypatch):
        monkeypatch.setattr(brain_service, "MAX_AGENTS", 1)
        await tenant_client.post("/brain/agents", json=AGENT)
        r = await tenant_client.post("/brain/agents", json={**AGENT, "name": "dos"})
        assert r.status_code == 400 and "no more than" in r.json()["detail"]

    async def test_another_users_agent_is_not_found(self, tenant_client, other_tenant):
        agent_id = "00000000-0000-4000-8000-0000000000aa"
        await _seed("brain_agents", {"id": agent_id, "user_id": other_tenant.user_id, **AGENT})
        assert (await tenant_client.put(f"/brain/agents/{agent_id}", json={})).status_code == 404
        assert (await tenant_client.delete(f"/brain/agents/{agent_id}")).status_code == 404
        r = await tenant_client.post(
            f"/brain/agents/{agent_id}/run", json={"kwami_id": other_tenant.kwami_id}
        )
        assert r.status_code == 404
        assert len(await _rows("brain_agents")) == 1

    async def test_running_an_agent_queues_its_job_where_it_works(self, tenant_client, tenant):
        agent = (await tenant_client.post("/brain/agents", json={**AGENT, "target": "pc"})).json()
        r = await tenant_client.post(
            f"/brain/agents/{agent['id']}/run", json={"kwami_id": tenant.kwami_id}
        )
        assert r.status_code == 200
        assert r.json()["target"] == "pc" and r.json()["where"] == "waiting"
        row = (await _rows("brain_jobs"))[0]
        assert row["agent_id"] == agent["id"]
        assert row["task"] == f"Haz tu trabajo de siempre: {AGENT['description']}"
        assert row["title"].startswith("facturas: ")

        r = await tenant_client.post(
            f"/brain/agents/{agent['id']}/run",
            json={"kwami_id": tenant.kwami_id, "task": "Solo las de octubre"},
        )
        assert (await _rows("brain_jobs"))[1]["task"] == "Solo las de octubre"

    async def test_a_job_can_name_an_agent_directly(self, tenant_client, tenant):
        agent = (
            await tenant_client.post("/brain/agents", json={**AGENT, "target": "cloud"})
        ).json()
        body = {"kwami_id": tenant.kwami_id, "task": "x", "agent_id": agent["id"]}
        assert (await tenant_client.post("/brain/jobs", json=body)).json()["target"] == "cloud"
        chosen = {**body, "target": "pc"}
        assert (await tenant_client.post("/brain/jobs", json=chosen)).json()["target"] == "pc"
        missing = {**body, "agent_id": "00000000-0000-4000-8000-0000000000bb"}
        assert (await tenant_client.post("/brain/jobs", json=missing)).status_code == 404

    async def test_a_switched_off_agent_does_not_run(self, tenant_client, tenant):
        agent = (await tenant_client.post("/brain/agents", json={**AGENT, "enabled": False})).json()
        r = await tenant_client.post(
            f"/brain/agents/{agent['id']}/run", json={"kwami_id": tenant.kwami_id}
        )
        assert r.status_code == 400

    async def test_running_with_a_full_queue_is_a_429(self, tenant_client, tenant, monkeypatch):
        agent = (await tenant_client.post("/brain/agents", json=AGENT)).json()
        monkeypatch.setattr(brain_service, "MAX_QUEUED", 0)
        r = await tenant_client.post(
            f"/brain/agents/{agent['id']}/run", json={"kwami_id": tenant.kwami_id}
        )
        assert r.status_code == 429

    async def test_an_id_that_is_no_uuid_is_simply_not_found(self, tenant):
        with pytest.raises(brain_service.AgentNotFoundError):
            await brain_service.get_agent(tenant.user_id, "not-a-uuid")


class TestSync:
    async def test_the_list_and_discarding_what_waits(self, tenant_client, tenant, other_tenant):
        sync_id = "00000000-0000-4000-8000-0000000000cc"
        await _seed(
            "brain_sync",
            {
                "id": sync_id,
                "user_id": tenant.user_id,
                "status": "pending",
                "storage_prefix": "u/k/t",
                "created_at": _now(),
            },
        )
        await _seed(
            "brain_sync",
            {
                "id": "theirs",
                "user_id": other_tenant.user_id,
                "status": "pending",
                "created_at": _now(),
            },
        )
        items = (await tenant_client.get("/brain/sync")).json()["items"]
        assert [i["id"] for i in items] == [sync_id]
        assert "storage_prefix" not in items[0]

        assert (await tenant_client.post(f"/brain/sync/{sync_id}/discard")).json() == {
            "discarded": True
        }
        assert (await tenant_client.post(f"/brain/sync/{sync_id}/discard")).status_code == 404


class TestWithoutMigration:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "/brain/where", None),
            ("put", "/brain/cloud", {"enabled": True}),
            ("get", "/brain/jobs", None),
            ("post", "/brain/jobs/00000000-0000-4000-8000-000000000001/cancel", None),
            ("get", "/brain/agents", None),
            ("post", "/brain/agents", AGENT),
            ("put", "/brain/agents/00000000-0000-4000-8000-000000000001", {}),
            ("delete", "/brain/agents/00000000-0000-4000-8000-000000000001", None),
            ("get", "/brain/sync", None),
            ("post", "/brain/sync/00000000-0000-4000-8000-000000000001/discard", None),
        ],
    )
    async def test_every_route_says_503(self, tenant_client, monkeypatch, method, path, body):
        monkeypatch.setattr(brain_service, "get_supabase_admin", _NotMigrated)
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(tenant_client, method)(path, **kwargs)
        assert r.status_code == 503

    async def test_asking_for_work_says_503(self, tenant_client, tenant, monkeypatch):
        monkeypatch.setattr(brain_service, "get_supabase_admin", _NotMigrated)
        r = await tenant_client.post("/brain/jobs", json={"kwami_id": tenant.kwami_id, "task": "x"})
        assert r.status_code == 503
        agent_id = "00000000-0000-4000-8000-000000000001"
        r = await tenant_client.post(
            f"/brain/agents/{agent_id}/run", json={"kwami_id": tenant.kwami_id}
        )
        assert r.status_code == 503

    async def test_other_database_errors_are_not_hidden(self, tenant, monkeypatch):
        monkeypatch.setattr(brain_service, "get_supabase_admin", _Broken)
        with pytest.raises(APIError):
            await brain_service.cloud_online()
