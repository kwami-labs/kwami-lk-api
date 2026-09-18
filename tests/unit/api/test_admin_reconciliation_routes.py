"""`/admin/reconciliation` — the admin surface over provider imports and runs.

Every route is admin-gated two ways (`X-Admin-API-Key` or an allowlisted user),
and the guard itself is covered in tests/unit/core/test_security_and_deps.py.
What is here is the route layer: how service errors map to status codes, and how
the acting admin is recorded on each import — `imported_by` is the audit trail,
so its fallback order matters.
"""

from __future__ import annotations

import pytest

from src.api.routes import admin_reconciliation as routes
from src.core.security import AdminPrincipal
from src.services import admin_reconciliation as ar

pytestmark = pytest.mark.anyio

ADMIN_KEY = "admin-secret"
BASE = "/admin/reconciliation"


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    from src.core import security

    monkeypatch.setattr(security.settings, "admin_api_key", ADMIN_KEY, raising=False)


@pytest.fixture
def admin_client(client):
    client.headers["X-Admin-API-Key"] = ADMIN_KEY
    return client


def _manual_body(**overrides):
    return {
        "provider": "tavily",
        "lines": [
            {
                "service": "search",
                "usage_unit": "credits",
                "usage_quantity": 12,
                "raw_cost_usd": 0.096,
            }
        ],
        **overrides,
    }


class TestImportedBy:
    """The audit trail: email, then user id, then how they authenticated."""

    def test_an_email_wins(self):
        principal = AdminPrincipal(auth_method="user", user_id="u1", email="a@b.c")
        assert routes._imported_by(principal) == "a@b.c"

    def test_the_user_id_is_next(self):
        assert routes._imported_by(AdminPrincipal(auth_method="user", user_id="u1")) == "u1"

    def test_the_auth_method_is_the_last_resort(self):
        assert routes._imported_by(AdminPrincipal(auth_method="api_key")) == "api_key"


class TestAuthGuard:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", f"{BASE}/imports/manual"),
            ("post", f"{BASE}/imports/pull"),
            ("get", f"{BASE}/imports"),
            ("get", f"{BASE}/imports/imp-1"),
            ("post", f"{BASE}/runs"),
            ("get", f"{BASE}/runs"),
            ("get", f"{BASE}/runs/run-1"),
            ("get", f"{BASE}/runs/run-1/findings"),
        ],
    )
    async def test_every_route_needs_an_admin(self, client, method, path):
        kwargs = {"json": {}} if method == "post" else {}
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code == 403

    async def test_a_wrong_key_is_refused(self, client):
        client.headers["X-Admin-API-Key"] = "not-the-key"
        assert (await client.get(f"{BASE}/imports")).status_code == 403


class TestManualImport:
    async def test_it_creates_an_import(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/imports/manual", json=_manual_body())
        assert r.status_code == 200
        body = r.json()
        assert body["import_id"]
        assert body["summary"]["lines_count"] == 1
        assert body["summary"]["raw_cost_usd"] == 0.096

    async def test_the_acting_admin_is_recorded(self, admin_client, fake_supabase):
        await admin_client.post(f"{BASE}/imports/manual", json=_manual_body())
        assert fake_supabase.db.rows("provider_usage_imports")[0]["imported_by"] == "api_key"

    async def test_an_unsupported_provider_is_a_400(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/imports/manual", json=_manual_body(provider="stripe"))
        assert r.status_code == 400
        assert "Unsupported provider" in r.json()["detail"]

    async def test_a_runtime_failure_is_a_503(self, monkeypatch, admin_client, caplog):
        async def boom(**kwargs):
            raise RuntimeError("supabase unavailable")

        monkeypatch.setattr(routes, "import_provider_usage_manual", boom)
        with caplog.at_level("ERROR", logger="kwami-api.admin-reconciliation"):
            r = await admin_client.post(f"{BASE}/imports/manual", json=_manual_body())
        assert r.status_code == 503
        assert "Manual provider import failed" in caplog.text

    async def test_the_period_and_reference_are_carried(self, admin_client, fake_supabase):
        await admin_client.post(
            f"{BASE}/imports/manual",
            json=_manual_body(
                invoice_period_start="2026-03-01T00:00:00Z",
                invoice_period_end="2026-03-31T00:00:00Z",
                external_reference="inv-1",
                source_label="csv upload",
            ),
        )
        row = fake_supabase.db.rows("provider_usage_imports")[0]
        assert row["external_reference"] == "inv-1"
        assert row["source_label"] == "csv upload"

    async def test_an_empty_line_list_is_accepted(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/imports/manual", json=_manual_body(lines=[]))
        assert r.status_code == 200
        assert r.json()["summary"]["lines_count"] == 0

    async def test_a_malformed_line_is_a_422(self, admin_client):
        r = await admin_client.post(
            f"{BASE}/imports/manual", json=_manual_body(lines=[{"service": "search"}])
        )
        assert r.status_code == 422


class TestApiPullImport:
    def _body(self, **overrides):
        return {
            "provider": "openai",
            "invoice_period_start": "2026-03-01T00:00:00Z",
            "invoice_period_end": "2026-03-31T00:00:00Z",
            **overrides,
        }

    async def test_it_pulls_and_imports(self, monkeypatch, admin_client, fake_supabase):
        monkeypatch.setattr(
            ar,
            "pull_provider_usage",
            lambda *a, **k: {
                "source_label": "openai.organization.costs",
                "summary": {"pages": 1},
                "raw_payload": {},
                "lines": [],
            },
        )
        r = await admin_client.post(f"{BASE}/imports/pull", json=self._body())
        assert r.status_code == 200
        assert r.json()["source_label"] == "openai.organization.costs"

    async def test_the_options_reach_the_puller(self, monkeypatch, admin_client, fake_supabase):
        seen: dict = {}
        monkeypatch.setattr(
            ar,
            "pull_provider_usage",
            lambda p, s, e, *, options: (
                seen.update(options=options)
                or {"source_label": "x", "summary": {}, "raw_payload": {}, "lines": []}
            ),
        )
        await admin_client.post(
            f"{BASE}/imports/pull", json=self._body(options={"project_ids": ["p1"]})
        )
        assert seen["options"] == {"project_ids": ["p1"]}

    async def test_an_unsupported_provider_is_a_400(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/imports/pull", json=self._body(provider="stripe"))
        assert r.status_code == 400

    async def test_a_provider_api_failure_is_a_503(
        self, monkeypatch, admin_client, fake_supabase, caplog
    ):
        def boom(*a, **k):
            raise RuntimeError("OPENAI_ADMIN_KEY is required")

        monkeypatch.setattr(ar, "pull_provider_usage", boom)
        with caplog.at_level("ERROR", logger="kwami-api.admin-reconciliation"):
            r = await admin_client.post(f"{BASE}/imports/pull", json=self._body())
        assert r.status_code == 503
        assert "Provider pull failed" in caplog.text

    async def test_the_period_is_required(self, admin_client):
        r = await admin_client.post(f"{BASE}/imports/pull", json={"provider": "openai"})
        assert r.status_code == 422


class TestListAndGetImports:
    async def _seed(self, admin_client, provider="tavily"):
        r = await admin_client.post(f"{BASE}/imports/manual", json=_manual_body(provider=provider))
        return r.json()["import_id"]

    async def test_listing(self, admin_client, fake_supabase):
        await self._seed(admin_client)
        r = await admin_client.get(f"{BASE}/imports")
        assert r.status_code == 200
        assert r.json()["count"] == 1

    async def test_an_empty_list(self, admin_client, fake_supabase):
        r = await admin_client.get(f"{BASE}/imports")
        assert r.json() == {"imports": [], "count": 0}

    async def test_filtering_by_provider(self, admin_client, fake_supabase):
        await self._seed(admin_client, provider="tavily")
        await self._seed(admin_client, provider="openai")
        r = await admin_client.get(f"{BASE}/imports", params={"provider": "openai"})
        assert r.json()["count"] == 1

    async def test_an_unsupported_provider_filter_is_a_400(self, admin_client, fake_supabase):
        r = await admin_client.get(f"{BASE}/imports", params={"provider": "stripe"})
        assert r.status_code == 400

    @pytest.mark.parametrize("limit", [0, 501])
    async def test_an_out_of_range_limit_is_a_422(self, admin_client, limit):
        r = await admin_client.get(f"{BASE}/imports", params={"limit": limit})
        assert r.status_code == 422

    async def test_getting_a_detail(self, admin_client, fake_supabase):
        import_id = await self._seed(admin_client)
        r = await admin_client.get(f"{BASE}/imports/{import_id}")
        assert r.status_code == 200
        body = r.json()
        # The field is `import_record` in Python (a keyword) but is declared with
        # `alias="import"`, and FastAPI serializes by alias -- so that is the key
        # clients actually see, matching what the service returns.
        assert body["import"]["provider"] == "tavily"
        assert "import_record" not in body
        assert len(body["lines"]) == 1

    async def test_an_unknown_import_is_a_404(self, admin_client, fake_supabase):
        r = await admin_client.get(f"{BASE}/imports/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404
        assert r.json()["detail"] == "Provider import not found"


class TestRuns:
    async def test_creating_a_run(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/runs", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"]
        assert "providers" in body["summary"]
        assert body["findings"] == []

    async def test_the_filters_are_carried(self, admin_client, fake_supabase):
        r = await admin_client.post(
            f"{BASE}/runs",
            json={
                "provider_filters": ["openai"],
                "import_ids": ["imp-1"],
                "period_start": "2026-03-01T00:00:00Z",
                "period_end": "2026-03-31T00:00:00Z",
            },
        )
        assert r.status_code == 200
        row = fake_supabase.db.rows("provider_reconciliation_runs")[0]
        assert row["provider_filters"] == ["openai"]
        assert row["import_ids"] == ["imp-1"]

    async def test_an_unsupported_filter_is_a_400(self, admin_client, fake_supabase):
        r = await admin_client.post(f"{BASE}/runs", json={"provider_filters": ["stripe"]})
        assert r.status_code == 400

    async def test_a_runtime_failure_is_a_503(self, monkeypatch, admin_client, caplog):
        async def boom(**kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(routes, "run_admin_reconciliation", boom)
        with caplog.at_level("ERROR", logger="kwami-api.admin-reconciliation"):
            r = await admin_client.post(f"{BASE}/runs", json={})
        assert r.status_code == 503
        assert "Reconciliation run failed" in caplog.text

    async def test_listing_runs(self, admin_client, fake_supabase):
        await admin_client.post(f"{BASE}/runs", json={})
        r = await admin_client.get(f"{BASE}/runs")
        assert r.status_code == 200
        assert r.json()["count"] == 1

    async def test_an_empty_run_list(self, admin_client, fake_supabase):
        assert (await admin_client.get(f"{BASE}/runs")).json() == {"runs": [], "count": 0}

    @pytest.mark.parametrize("limit", [0, 201])
    async def test_an_out_of_range_limit_is_a_422(self, admin_client, limit):
        r = await admin_client.get(f"{BASE}/runs", params={"limit": limit})
        assert r.status_code == 422

    async def test_getting_a_run_detail(self, admin_client, fake_supabase):
        run_id = (await admin_client.post(f"{BASE}/runs", json={})).json()["run_id"]
        r = await admin_client.get(f"{BASE}/runs/{run_id}")
        assert r.status_code == 200
        assert r.json()["run"]["status"] == "completed"

    async def test_an_unknown_run_is_a_404(self, admin_client, fake_supabase):
        r = await admin_client.get(f"{BASE}/runs/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404
        assert r.json()["detail"] == "Reconciliation run not found"

    async def test_getting_the_findings(self, admin_client, fake_supabase):
        fake_supabase.db.seed(
            "credit_usage_logs",
            {
                "user_id": "u1",
                "session_id": "room-1",
                "model_id": "openai/gpt-4o",
                "model_type": "llm",
                "units_used": 1,
                "provider_cost_usd": 0.0,
                "credits_charged": 0,
            },
        )
        run_id = (await admin_client.post(f"{BASE}/runs", json={})).json()["run_id"]
        r = await admin_client.get(f"{BASE}/runs/{run_id}/findings")
        assert r.status_code == 200
        assert any(f["finding_type"] == "zero_provider_cost" for f in r.json())

    async def test_findings_for_an_unknown_run_is_a_404(self, admin_client, fake_supabase):
        r = await admin_client.get(f"{BASE}/runs/00000000-0000-0000-0000-000000000000/findings")
        assert r.status_code == 404
