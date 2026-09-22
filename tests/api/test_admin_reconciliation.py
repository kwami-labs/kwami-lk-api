import pytest
from httpx import AsyncClient

from src.api.routes import admin_reconciliation as admin_reconciliation_routes
from src.core.config import settings
from src.services import admin_reconciliation


def test_normalize_manual_import_lines():
    lines = admin_reconciliation.normalize_manual_import_lines(
        "tavily",
        [
            {
                "service": "search",
                "usage_unit": "credits",
                "usage_quantity": 12,
                "raw_cost_usd": 0.096,
                "session_id": "room-1",
                "metadata": {"source": "csv"},
            }
        ],
    )

    assert len(lines) == 1
    assert lines[0].provider == "tavily"
    assert lines[0].service == "search"
    assert lines[0].raw_cost_usd == 0.096
    assert lines[0].session_id == "room-1"


@pytest.mark.anyio
async def test_admin_manual_import_requires_admin(client: AsyncClient):
    response = await client.post(
        "/admin/reconciliation/imports/manual",
        json={
            "provider": "openai",
            "lines": [
                {
                    "service": "api",
                    "usage_unit": "usd",
                    "usage_quantity": 1,
                    "raw_cost_usd": 1.25,
                }
            ],
        },
    )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_admin_manual_import_accepts_admin_api_key(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "super-secret-admin-key")

    async def fake_manual_import(**kwargs):
        assert kwargs["provider"] == "openai"
        return {
            "import_id": "import-123",
            "summary": {"lines_count": 1},
            "source_label": "manual_import",
        }

    monkeypatch.setattr(
        admin_reconciliation_routes,
        "import_provider_usage_manual",
        fake_manual_import,
    )

    response = await client.post(
        "/admin/reconciliation/imports/manual",
        headers={"X-Admin-API-Key": "super-secret-admin-key"},
        json={
            "provider": "openai",
            "lines": [
                {
                    "service": "api",
                    "usage_unit": "usd",
                    "usage_quantity": 1,
                    "raw_cost_usd": 1.25,
                }
            ],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["import_id"] == "import-123"
    assert data["summary"]["lines_count"] == 1


@pytest.mark.anyio
async def test_run_admin_reconciliation_builds_provider_delta(monkeypatch):
    findings_written = {}
    finalized = {}

    async def fake_create_run(**kwargs):
        return "run-123"

    async def fake_replace_findings(run_id, findings):
        findings_written["run_id"] = run_id
        findings_written["findings"] = findings

    async def fake_finalize_run(run_id, *, status, summary=None, error=None):
        finalized["run_id"] = run_id
        finalized["status"] = status
        finalized["summary"] = summary
        finalized["error"] = error

    monkeypatch.setattr(admin_reconciliation, "create_reconciliation_run", fake_create_run)
    monkeypatch.setattr(
        admin_reconciliation, "replace_reconciliation_findings", fake_replace_findings
    )
    monkeypatch.setattr(admin_reconciliation, "finalize_reconciliation_run", fake_finalize_run)

    async def fake_fetch_rows(table_name, **kwargs):
        return (
            [
                {
                    "provider": "openai",
                    "service": "api",
                    "raw_cost_usd": 2.0,
                    "estimated_cost_usd": None,
                    "session_id": "session-1",
                    "external_reference": None,
                    "resource_id": "proj_1",
                    "import_id": "import-1",
                    "started_at": "2026-03-01T00:00:00+00:00",
                }
            ]
            if table_name == "provider_usage_lines"
            else [
                {
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "model_id": "openai/gpt-4o-mini",
                    "model_type": "llm",
                    "provider_cost_usd": 1.5,
                    "billed_cost_usd": 3.0,
                    "pricing_source": "catalog:2026-03-20",
                    "created_at": "2026-03-01T00:00:00+00:00",
                }
            ]
        )

    monkeypatch.setattr(admin_reconciliation, "_fetch_rows", fake_fetch_rows)

    result = await admin_reconciliation.run_admin_reconciliation(
        provider_filters=["openai"],
        import_ids=[],
        period_start=None,
        period_end=None,
        created_by="admin@example.com",
    )

    assert result["run_id"] == "run-123"
    provider_summary = result["summary"]["providers"][0]
    assert provider_summary["provider"] == "openai"
    assert provider_summary["imported_cost_usd"] == 2.0
    assert provider_summary["internal_provider_cost_usd"] == 1.5
    assert provider_summary["internal_billed_cost_usd"] == 3.0
    assert provider_summary["realized_margin_usd"] == 1.0
    assert finalized["status"] == "completed"
    assert findings_written["run_id"] == "run-123"
