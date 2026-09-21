"""Admin-only provider invoice reconciliation endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_admin
from src.core.security import AdminPrincipal
from src.services.admin_reconciliation import (
    get_provider_usage_import,
    get_reconciliation_run,
    import_provider_usage_from_api,
    import_provider_usage_manual,
    list_provider_usage_imports,
    list_reconciliation_runs,
    run_admin_reconciliation,
)

logger = logging.getLogger("kwami-api.admin-reconciliation")
router = APIRouter()


class ManualImportLine(BaseModel):
    service: str
    usage_unit: str
    usage_quantity: float = 0
    raw_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    currency: str = "usd"
    resource_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    external_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_line: dict[str, Any] | None = None


class ManualImportRequest(BaseModel):
    provider: str
    source_label: str | None = None
    invoice_period_start: datetime | None = None
    invoice_period_end: datetime | None = None
    external_reference: str | None = None
    lines: list[ManualImportLine]


class ApiPullImportRequest(BaseModel):
    provider: str
    invoice_period_start: datetime
    invoice_period_end: datetime
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderImportResponse(BaseModel):
    import_id: str
    summary: dict[str, Any]
    source_label: str | None = None


class ProviderImportListResponse(BaseModel):
    imports: list[dict[str, Any]]
    count: int


class ProviderImportDetailResponse(BaseModel):
    import_record: dict[str, Any] = Field(alias="import")
    lines: list[dict[str, Any]]


class ReconciliationRunRequest(BaseModel):
    provider_filters: list[str] = Field(default_factory=list)
    import_ids: list[str] = Field(default_factory=list)
    period_start: datetime | None = None
    period_end: datetime | None = None


class ReconciliationRunResponse(BaseModel):
    run_id: str
    summary: dict[str, Any]
    findings: list[dict[str, Any]]


class ReconciliationRunListResponse(BaseModel):
    runs: list[dict[str, Any]]
    count: int


class ReconciliationRunDetailResponse(BaseModel):
    run: dict[str, Any]
    findings: list[dict[str, Any]]


def _imported_by(admin: AdminPrincipal) -> str:
    if admin.email:
        return admin.email
    if admin.user_id:
        return admin.user_id
    return admin.auth_method


@router.post("/imports/manual", response_model=ProviderImportResponse)
async def create_manual_provider_import(
    request: ManualImportRequest,
    admin: Annotated[AdminPrincipal, Depends(require_admin)],
):
    """Create a manual provider usage or invoice import."""
    try:
        result = await import_provider_usage_manual(
            provider=request.provider,
            source_label=request.source_label,
            invoice_period_start=request.invoice_period_start,
            invoice_period_end=request.invoice_period_end,
            external_reference=request.external_reference,
            imported_by=_imported_by(admin),
            lines=[line.model_dump() for line in request.lines],
        )
        return ProviderImportResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Manual provider import failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/imports/pull", response_model=ProviderImportResponse)
async def pull_provider_import(
    request: ApiPullImportRequest,
    admin: Annotated[AdminPrincipal, Depends(require_admin)],
):
    """Pull provider usage directly from a provider API."""
    try:
        result = await import_provider_usage_from_api(
            provider=request.provider,
            invoice_period_start=request.invoice_period_start,
            invoice_period_end=request.invoice_period_end,
            imported_by=_imported_by(admin),
            options=request.options,
        )
        return ProviderImportResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Provider pull failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/imports", response_model=ProviderImportListResponse)
async def get_provider_imports(
    _: Annotated[AdminPrincipal, Depends(require_admin)],
    provider: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    try:
        imports = await list_provider_usage_imports(limit=limit, provider=provider)
        return ProviderImportListResponse(imports=imports, count=len(imports))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/imports/{import_id}", response_model=ProviderImportDetailResponse)
async def get_provider_import_detail(
    import_id: str,
    _: Annotated[AdminPrincipal, Depends(require_admin)],
):
    try:
        detail = await get_provider_usage_import(import_id)
        return ProviderImportDetailResponse(**detail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs", response_model=ReconciliationRunResponse)
async def create_reconciliation_run_endpoint(
    request: ReconciliationRunRequest,
    admin: Annotated[AdminPrincipal, Depends(require_admin)],
):
    """Run admin reconciliation against imported provider usage and the ledger."""
    try:
        result = await run_admin_reconciliation(
            provider_filters=request.provider_filters,
            import_ids=request.import_ids,
            period_start=request.period_start,
            period_end=request.period_end,
            created_by=_imported_by(admin),
        )
        return ReconciliationRunResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Reconciliation run failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/runs", response_model=ReconciliationRunListResponse)
async def get_reconciliation_runs(
    _: Annotated[AdminPrincipal, Depends(require_admin)],
    limit: int = Query(25, ge=1, le=200),
):
    runs = await list_reconciliation_runs(limit=limit)
    return ReconciliationRunListResponse(runs=runs, count=len(runs))


@router.get("/runs/{run_id}", response_model=ReconciliationRunDetailResponse)
async def get_reconciliation_run_detail(
    run_id: str,
    _: Annotated[AdminPrincipal, Depends(require_admin)],
):
    try:
        detail = await get_reconciliation_run(run_id)
        return ReconciliationRunDetailResponse(**detail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/findings", response_model=list[dict[str, Any]])
async def get_reconciliation_findings(
    run_id: str,
    _: Annotated[AdminPrincipal, Depends(require_admin)],
):
    try:
        detail = await get_reconciliation_run(run_id)
        return detail["findings"]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
