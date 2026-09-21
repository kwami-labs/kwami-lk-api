"""Admin-only provider invoice reconciliation services."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from livekit import api as livekit_api

from src.core.config import settings
from src.services.credits import get_supabase_admin
from src.services.pricing import ALL_PRICING

logger = logging.getLogger("kwami-api.admin-reconciliation")

# Largest detail page an admin endpoint will return for one import or run.
# Above PostgREST's default cap, so the bound is this service's rather than a
# silent property of whichever gateway is in front of the database.
MAX_DETAIL_ROWS = 5000

SUPPORTED_PROVIDERS = {"livekit", "openai", "tavily", "zep"}


@dataclass(slots=True)
class ProviderUsageLine:
    """Normalized provider usage or invoice line."""

    provider: str
    service: str
    usage_unit: str
    usage_quantity: float = 0.0
    raw_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    currency: str = "usd"
    resource_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    external_reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_line: dict[str, Any] = field(default_factory=dict)


def _round_usd(value: float) -> float:
    return round(value, 6)


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    return normalized


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.endswith("Z"):
            stripped = stripped.replace("Z", "+00:00")
        return datetime.fromisoformat(stripped)
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _provider_cost(line: dict[str, Any]) -> float:
    raw_cost = line.get("raw_cost_usd")
    if raw_cost is not None:
        return float(raw_cost)
    estimated_cost = line.get("estimated_cost_usd")
    if estimated_cost is not None:
        return float(estimated_cost)
    return 0.0


# Provider APIs are slow and paginated, and a reconciliation run walks several of
# them. The previous implementation used `urllib.request.urlopen(timeout=30)` from
# inside `async def` route handlers, so one run could hold the event loop for
# thirty seconds per page -- every other request on the worker, including Twilio
# webhooks and token issuance, waited behind it.
PROVIDER_REQUEST_TIMEOUT_SECONDS = 30.0


async def _json_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform a JSON GET against a provider API, without blocking the loop."""
    # Only https: the bases are operator-configured, but `urlopen` would happily
    # have followed a `file:` URL out of a mistyped setting (ruff S310).
    if not url.startswith("https://"):
        raise RuntimeError(f"Provider API URL must be https, got {url!r}")

    params = {k: v for k, v in (query or {}).items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers or {}, params=params or None)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Provider API request failed: {exc}") from exc

    if response.status_code >= 400:
        body = response.text[:500]
        raise RuntimeError(f"Provider API request failed ({response.status_code}): {body}")
    return response.json()


def _build_livekit_analytics_token() -> str:
    if settings.livekit_analytics_token:
        return settings.livekit_analytics_token
    token = (
        livekit_api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_ttl(timedelta(hours=1))
        .with_grants(livekit_api.VideoGrants(room_list=True))
    )
    return token.to_jwt()


async def _pull_openai_costs(
    period_start: datetime,
    period_end: datetime,
    *,
    project_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not settings.openai_admin_key:
        raise RuntimeError("OPENAI_ADMIN_KEY is required for OpenAI reconciliation pulls")

    headers = {
        "Authorization": f"Bearer {settings.openai_admin_key}",
        "Content-Type": "application/json",
    }
    lines: list[ProviderUsageLine] = []
    raw_pages: list[dict[str, Any]] = []
    next_page: str | None = None

    while True:
        payload = await _json_request(
            f"{settings.openai_api_base}/organization/costs",
            headers=headers,
            query={
                "start_time": int(period_start.timestamp()),
                "end_time": int(period_end.timestamp()),
                "bucket_width": "1d",
                "group_by": ["project_id", "line_item"],
                "limit": 31,
                "page": next_page,
                "project_ids": project_ids or None,
            },
        )
        raw_pages.append(payload)
        for bucket in payload.get("data", []):
            results = bucket.get("results") or bucket.get("result") or []
            bucket_start = _parse_datetime(bucket.get("start_time"))
            bucket_end = _parse_datetime(bucket.get("end_time"))
            for result in results:
                amount = result.get("amount") or {}
                amount_value = amount.get("value")
                if amount_value is None:
                    continue
                project_id = result.get("project_id")
                line_item = result.get("line_item") or "openai_api"
                lines.append(
                    ProviderUsageLine(
                        provider="openai",
                        service=str(line_item),
                        usage_unit="usd",
                        usage_quantity=float(amount_value),
                        raw_cost_usd=float(amount_value),
                        currency=(amount.get("currency") or "usd").lower(),
                        resource_id=project_id,
                        external_reference=project_id,
                        started_at=bucket_start,
                        ended_at=bucket_end,
                        metadata={
                            "project_id": project_id,
                            "line_item": line_item,
                        },
                        raw_line=result,
                    )
                )
        if not payload.get("has_more"):
            break
        next_page = payload.get("next_page")
        if not next_page:
            break

    return {
        "source_label": "openai.organization.costs",
        "summary": {
            "pages": len(raw_pages),
            "lines_count": len(lines),
        },
        "raw_payload": {"pages": raw_pages},
        "lines": lines,
    }


async def _pull_tavily_usage(
    period_start: datetime,
    period_end: datetime,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is required for Tavily reconciliation pulls")

    headers = {
        "Authorization": f"Bearer {settings.tavily_api_key}",
        "Content-Type": "application/json",
    }
    if project_id or settings.tavily_project_id:
        headers["X-Project-ID"] = project_id or settings.tavily_project_id or ""

    payload = await _json_request(
        "https://api.tavily.com/usage",
        headers=headers,
    )
    account = payload.get("account") or {}
    lines: list[ProviderUsageLine] = []
    for service in ("search", "extract", "crawl", "map", "research"):
        usage_credits = account.get(f"{service}_usage")
        if usage_credits in (None, 0):
            continue
        cost_usd = float(usage_credits) * settings.reconciliation_tavily_cost_per_credit_usd
        lines.append(
            ProviderUsageLine(
                provider="tavily",
                service=service,
                usage_unit="credits",
                usage_quantity=float(usage_credits),
                raw_cost_usd=_round_usd(cost_usd),
                currency="usd",
                started_at=period_start,
                ended_at=period_end,
                external_reference=headers.get("X-Project-ID"),
                metadata={
                    "current_plan": account.get("current_plan"),
                    "project_id": headers.get("X-Project-ID"),
                    "plan_usage": account.get("plan_usage"),
                },
                raw_line={"account": account, "service": service},
            )
        )

    return {
        "source_label": "tavily.usage",
        "summary": {
            "lines_count": len(lines),
            "plan_usage": account.get("plan_usage"),
            "plan_limit": account.get("plan_limit"),
        },
        "raw_payload": payload,
        "lines": lines,
    }


async def _pull_livekit_usage(
    period_start: datetime,
    period_end: datetime,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    if not settings.livekit_cloud_project_id:
        raise RuntimeError("LIVEKIT_CLOUD_PROJECT_ID is required for LiveKit reconciliation pulls")

    token = _build_livekit_analytics_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    project_id = settings.livekit_cloud_project_id
    page = 0
    sessions: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    while True:
        payload = await _json_request(
            f"{settings.livekit_cloud_api_base}/project/{project_id}/sessions",
            headers=headers,
            query={
                "page": page,
                "limit": limit,
                "start": period_start.date().isoformat(),
                "end": period_end.date().isoformat(),
            },
        )
        page_sessions = payload.get("sessions") or []
        sessions.extend(page_sessions)
        if len(page_sessions) < limit:
            break
        page += 1

    lines: list[ProviderUsageLine] = []
    for session in sessions:
        session_id = session.get("sessionId")
        detail = {}
        if session_id:
            try:
                detail = await _json_request(
                    f"{settings.livekit_cloud_api_base}/project/{project_id}/sessions/{session_id}",
                    headers=headers,
                )
                details.append(detail)
            except Exception as exc:
                logger.warning("LiveKit session detail lookup failed for %s: %s", session_id, exc)
        bandwidth_bytes = detail.get("bandwidth")
        if bandwidth_bytes is None:
            bandwidth_bytes = (session.get("bandwidthIn") or 0) + (session.get("bandwidthOut") or 0)
        bandwidth_gb = float(bandwidth_bytes or 0) / 1_000_000_000
        connection_minutes = float(detail.get("connectionMinutes") or 0)
        estimated_cost = (
            connection_minutes * settings.reconciliation_livekit_connection_minute_usd
            + bandwidth_gb * settings.reconciliation_livekit_bandwidth_gb_usd
        )
        lines.append(
            ProviderUsageLine(
                provider="livekit",
                service="cloud_session",
                usage_unit="connection_minutes",
                usage_quantity=connection_minutes,
                estimated_cost_usd=_round_usd(estimated_cost),
                currency="usd",
                resource_id=session_id,
                session_id=session_id,
                started_at=_parse_datetime(detail.get("startTime") or session.get("createdAt")),
                ended_at=_parse_datetime(detail.get("endTime") or session.get("endedAt")),
                external_reference=detail.get("roomName") or session.get("roomName"),
                metadata={
                    "bandwidth_bytes": bandwidth_bytes,
                    "bandwidth_gb": round(bandwidth_gb, 6),
                    "num_participants": detail.get("numParticipants")
                    or session.get("numParticipants"),
                    "pricing_note": "Estimated from analytics API using configured LiveKit rates.",
                },
                raw_line=detail or session,
            )
        )

    return {
        "source_label": "livekit.cloud.analytics",
        "summary": {
            "sessions_count": len(lines),
            "pages_fetched": page + 1,
        },
        "raw_payload": {
            "sessions": sessions,
            "details": details,
        },
        "lines": lines,
    }


def _normalize_zep_usage_payload(
    payload: Any, period_start: datetime, period_end: datetime
) -> list[ProviderUsageLine]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_items = payload.get("data", [])
        elif isinstance(payload.get("usage"), list):
            raw_items = payload.get("usage", [])
        else:
            raw_items = [payload]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    lines: list[ProviderUsageLine] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        lines.append(
            ProviderUsageLine(
                provider="zep",
                service=str(item.get("service") or item.get("endpoint") or "usage"),
                usage_unit=str(item.get("usage_unit") or item.get("unit") or "request"),
                usage_quantity=float(item.get("usage_quantity") or item.get("count") or 1),
                raw_cost_usd=(
                    float(item["cost_usd"]) if item.get("cost_usd") is not None else None
                ),
                estimated_cost_usd=(
                    float(item["estimated_cost_usd"])
                    if item.get("estimated_cost_usd") is not None
                    else None
                ),
                currency=str(item.get("currency") or "usd").lower(),
                resource_id=item.get("project_id") or item.get("resource_id"),
                session_id=item.get("thread_id") or item.get("session_id"),
                user_id=item.get("user_id"),
                started_at=_parse_datetime(item.get("started_at")) or period_start,
                ended_at=_parse_datetime(item.get("ended_at")) or period_end,
                external_reference=item.get("external_reference"),
                metadata={
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "service",
                        "endpoint",
                        "usage_unit",
                        "unit",
                        "usage_quantity",
                        "count",
                        "cost_usd",
                        "estimated_cost_usd",
                        "currency",
                        "project_id",
                        "resource_id",
                        "thread_id",
                        "session_id",
                        "user_id",
                        "started_at",
                        "ended_at",
                        "external_reference",
                    }
                },
                raw_line=item,
            )
        )
    return lines


async def _pull_zep_usage(period_start: datetime, period_end: datetime) -> dict[str, Any]:
    if not settings.zep_api_key:
        raise RuntimeError("ZEP_API_KEY is required for Zep reconciliation pulls")

    headers = {
        "Authorization": f"Bearer {settings.zep_api_key}",
        "Content-Type": "application/json",
    }
    project_info = await _json_request(
        f"{settings.zep_api_base.rstrip('/')}/projects/info",
        headers=headers,
    )

    payload: dict[str, Any] = {"project_info": project_info}
    lines: list[ProviderUsageLine] = []
    source_label = "zep.project_info"
    summary: dict[str, Any] = {"project": project_info.get("project", {})}

    if settings.zep_usage_api_url:
        usage_payload = await _json_request(
            settings.zep_usage_api_url,
            headers=headers,
        )
        payload["usage"] = usage_payload
        lines = _normalize_zep_usage_payload(usage_payload, period_start, period_end)
        source_label = "zep.usage_api"
        summary["lines_count"] = len(lines)
    else:
        summary["warning"] = (
            "Zep does not expose a standard usage billing endpoint here. "
            "Use manual imports for exact spend or configure ZEP_USAGE_API_URL."
        )

    return {
        "source_label": source_label,
        "summary": summary,
        "raw_payload": payload,
        "lines": lines,
    }


async def pull_provider_usage(
    provider: str,
    period_start: datetime,
    period_end: datetime,
    *,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pull provider usage via the provider's API when available."""
    normalized_provider = _normalize_provider(provider)
    options = options or {}
    match normalized_provider:
        case "openai":
            return await _pull_openai_costs(
                period_start,
                period_end,
                project_ids=options.get("project_ids"),
            )
        case "tavily":
            return await _pull_tavily_usage(
                period_start,
                period_end,
                project_id=options.get("project_id"),
            )
        case "livekit":
            return await _pull_livekit_usage(
                period_start,
                period_end,
                limit=int(options.get("limit") or 100),
            )
        case "zep":
            return await _pull_zep_usage(period_start, period_end)
    raise ValueError(f"Unsupported provider: {provider}")


def normalize_manual_import_lines(
    provider: str,
    lines: list[dict[str, Any]],
) -> list[ProviderUsageLine]:
    """Normalize manually supplied invoice or export lines."""
    normalized_provider = _normalize_provider(provider)
    normalized_lines: list[ProviderUsageLine] = []
    for line in lines:
        normalized_lines.append(
            ProviderUsageLine(
                provider=normalized_provider,
                service=str(line.get("service") or "manual"),
                usage_unit=str(line.get("usage_unit") or "unit"),
                usage_quantity=float(line.get("usage_quantity") or 0),
                raw_cost_usd=(
                    float(line["raw_cost_usd"]) if line.get("raw_cost_usd") is not None else None
                ),
                estimated_cost_usd=(
                    float(line["estimated_cost_usd"])
                    if line.get("estimated_cost_usd") is not None
                    else None
                ),
                currency=str(line.get("currency") or "usd").lower(),
                resource_id=line.get("resource_id"),
                session_id=line.get("session_id"),
                user_id=line.get("user_id"),
                started_at=_parse_datetime(line.get("started_at")),
                ended_at=_parse_datetime(line.get("ended_at")),
                external_reference=line.get("external_reference"),
                metadata=dict(line.get("metadata") or {}),
                raw_line=dict(line.get("raw_line") or line),
            )
        )
    return normalized_lines


def _serialize_line(line: ProviderUsageLine, import_id: str) -> dict[str, Any]:
    return {
        "import_id": import_id,
        "provider": line.provider,
        "service": line.service,
        "usage_unit": line.usage_unit,
        "usage_quantity": line.usage_quantity,
        "raw_cost_usd": line.raw_cost_usd,
        "estimated_cost_usd": line.estimated_cost_usd,
        "currency": line.currency,
        "resource_id": line.resource_id,
        "session_id": line.session_id,
        "user_id": line.user_id,
        "started_at": _datetime_to_iso(line.started_at),
        "ended_at": _datetime_to_iso(line.ended_at),
        "external_reference": line.external_reference,
        "metadata": line.metadata,
        "raw_line": line.raw_line,
    }


async def _fetch_rows(
    table_name: str,
    *,
    order_column: str = "created_at",
    filters: list[tuple[str, str, Any]] | None = None,
    batch_size: int = 500,
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        query = sb.table(table_name).select("*").order(order_column, desc=True)
        for op, column, value in filters or []:
            if value is None:
                continue
            if op == "eq":
                query = query.eq(column, value)
            elif op == "gte":
                query = query.gte(column, value)
            elif op == "lte":
                query = query.lte(column, value)
            elif op == "in":
                query = query.in_(column, value)
        result = await query.range(offset, offset + batch_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size

    return rows


def _infer_internal_provider(model_id: str) -> str:
    pricing_entry = ALL_PRICING.get(model_id)
    if pricing_entry:
        return pricing_entry.provider
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    return "unknown"


async def create_provider_usage_import(
    *,
    provider: str,
    import_mode: str,
    source_label: str | None,
    invoice_period_start: datetime | None,
    invoice_period_end: datetime | None,
    currency: str = "usd",
    external_reference: str | None = None,
    imported_by: str | None = None,
) -> str:
    sb = get_supabase_admin()
    result = (
        await sb.table("provider_usage_imports")
        .insert(
            {
                "provider": _normalize_provider(provider),
                "import_mode": import_mode,
                "status": "pending",
                "source_label": source_label,
                "invoice_period_start": _datetime_to_iso(invoice_period_start),
                "invoice_period_end": _datetime_to_iso(invoice_period_end),
                "currency": currency.lower(),
                "external_reference": external_reference,
                "imported_by": imported_by,
            }
        )
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    raise RuntimeError("Failed to create provider usage import")


async def finalize_provider_usage_import(
    import_id: str,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    raw_payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    sb = get_supabase_admin()
    await (
        sb.table("provider_usage_imports")
        .update(
            {
                "status": status,
                "summary": summary or {},
                "raw_payload": raw_payload or {},
                "error": error,
            }
        )
        .eq("id", import_id)
        .execute()
    )


async def insert_provider_usage_lines(import_id: str, lines: list[ProviderUsageLine]) -> None:
    if not lines:
        return
    sb = get_supabase_admin()
    payload = [_serialize_line(line, import_id) for line in lines]
    await sb.table("provider_usage_lines").insert(payload).execute()


async def import_provider_usage_manual(
    *,
    provider: str,
    source_label: str | None,
    invoice_period_start: datetime | None,
    invoice_period_end: datetime | None,
    lines: list[dict[str, Any]],
    external_reference: str | None = None,
    imported_by: str | None = None,
) -> dict[str, Any]:
    normalized_lines = normalize_manual_import_lines(provider, lines)
    import_id = await create_provider_usage_import(
        provider=provider,
        import_mode="manual",
        source_label=source_label or "manual_import",
        invoice_period_start=invoice_period_start,
        invoice_period_end=invoice_period_end,
        external_reference=external_reference,
        imported_by=imported_by,
    )
    try:
        await insert_provider_usage_lines(import_id, normalized_lines)
        summary = {
            "lines_count": len(normalized_lines),
            "raw_cost_usd": _round_usd(
                sum(_provider_cost(asdict(line)) for line in normalized_lines)
            ),
        }
        await finalize_provider_usage_import(
            import_id,
            status="completed",
            summary=summary,
            raw_payload={"lines": [asdict(line) for line in normalized_lines]},
        )
        return {"import_id": import_id, "summary": summary}
    except Exception as exc:
        await finalize_provider_usage_import(
            import_id,
            status="failed",
            error=str(exc),
        )
        raise


async def import_provider_usage_from_api(
    *,
    provider: str,
    invoice_period_start: datetime,
    invoice_period_end: datetime,
    imported_by: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import_id = await create_provider_usage_import(
        provider=provider,
        import_mode="api_pull",
        source_label=f"{provider}.api_pull",
        invoice_period_start=invoice_period_start,
        invoice_period_end=invoice_period_end,
        imported_by=imported_by,
    )
    try:
        pulled = await pull_provider_usage(
            provider,
            invoice_period_start,
            invoice_period_end,
            options=options,
        )
        lines: list[ProviderUsageLine] = pulled["lines"]
        await insert_provider_usage_lines(import_id, lines)
        summary = dict(pulled.get("summary") or {})
        summary["lines_count"] = len(lines)
        await finalize_provider_usage_import(
            import_id,
            status="completed" if lines or provider != "zep" else "partial",
            summary=summary,
            raw_payload=dict(pulled.get("raw_payload") or {}),
        )
        return {
            "import_id": import_id,
            "source_label": pulled.get("source_label"),
            "summary": summary,
        }
    except Exception as exc:
        await finalize_provider_usage_import(
            import_id,
            status="failed",
            error=str(exc),
        )
        raise


async def list_provider_usage_imports(
    *,
    limit: int = 50,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    filters = [("eq", "provider", _normalize_provider(provider))] if provider else []
    rows = await _fetch_rows(
        "provider_usage_imports",
        filters=filters,
        batch_size=min(limit, 500),
    )
    return rows[:limit]


async def get_provider_usage_import(import_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    import_result = (
        await sb.table("provider_usage_imports").select("*").eq("id", import_id).execute()
    )
    if not import_result.data:
        raise ValueError("Provider import not found")
    line_result = (
        await sb.table("provider_usage_lines")
        .select("*")
        .eq("import_id", import_id)
        .order("created_at", desc=True)
        # An unbounded select is capped at PostgREST's own `max-rows` (1000 on
        # Supabase) with nothing in the response to say so, so a large invoice
        # came back quietly truncated. Bounding it here makes the ceiling
        # explicit and the same on every deployment.
        .limit(MAX_DETAIL_ROWS)
        .execute()
    )
    return {
        "import": import_result.data[0],
        "lines": line_result.data or [],
    }


def _filter_period_rows(
    rows: list[dict[str, Any]],
    *,
    started_at_key: str,
    period_start: datetime | None,
    period_end: datetime | None,
) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        row_time = _parse_datetime(row.get(started_at_key))
        if period_start and row_time and row_time < period_start:
            continue
        if period_end and row_time and row_time > period_end:
            continue
        filtered.append(row)
    return filtered


def _summarize_internal_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    provider_rows: dict[str, dict[str, Any]] = {}
    session_rows: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []

    for row in rows:
        provider = _infer_internal_provider(row.get("model_id") or "")
        provider_cost = float(row.get("provider_cost_usd") or row.get("cost_usd") or 0.0)
        billed_cost = float(row.get("billed_cost_usd") or provider_cost)
        session_id = row.get("session_id") or "unknown"
        service = row.get("model_type") or "unknown"

        provider_row = provider_rows.setdefault(
            provider,
            {
                "provider": provider,
                "provider_cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "usage_rows": 0,
                "session_ids": set(),
            },
        )
        provider_row["provider_cost_usd"] += provider_cost
        provider_row["billed_cost_usd"] += billed_cost
        provider_row["usage_rows"] += 1
        provider_row["session_ids"].add(session_id)

        session_row = session_rows.setdefault(
            session_id,
            {
                "session_id": session_id,
                "provider_cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "providers": set(),
            },
        )
        session_row["provider_cost_usd"] += provider_cost
        session_row["billed_cost_usd"] += billed_cost
        session_row["providers"].add(provider)

        if row.get("pricing_source") == "fallback":
            findings.append(
                {
                    "severity": "warning",
                    "finding_type": "fallback_pricing",
                    "provider": provider,
                    "service": service,
                    "session_id": session_id,
                    "user_id": row.get("user_id"),
                    "expected_cost_usd": provider_cost,
                    "actual_cost_usd": billed_cost,
                    "delta_cost_usd": billed_cost - provider_cost,
                    "metadata": {
                        "model_id": row.get("model_id"),
                        "pricing_source": row.get("pricing_source"),
                    },
                }
            )
        if provider_cost <= 0:
            findings.append(
                {
                    "severity": "warning",
                    "finding_type": "zero_provider_cost",
                    "provider": provider,
                    "service": service,
                    "session_id": session_id,
                    "user_id": row.get("user_id"),
                    "expected_cost_usd": 0.0,
                    "actual_cost_usd": billed_cost,
                    "delta_cost_usd": billed_cost,
                    "metadata": {
                        "model_id": row.get("model_id"),
                    },
                }
            )

    provider_list = []
    for row in provider_rows.values():
        provider_list.append(
            {
                "provider": row["provider"],
                "internal_provider_cost_usd": _round_usd(row["provider_cost_usd"]),
                "internal_billed_cost_usd": _round_usd(row["billed_cost_usd"]),
                "usage_rows": row["usage_rows"],
                "sessions_count": len(row["session_ids"]),
            }
        )
    provider_list.sort(key=lambda item: item["internal_provider_cost_usd"], reverse=True)

    return provider_list, {
        "by_session": session_rows,
        "findings": findings,
    }


def _summarize_imported_lines(
    lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    provider_rows: dict[str, dict[str, Any]] = {}
    matched_session_ids: set[str] = set()

    for line in lines:
        provider = line["provider"]
        service = line["service"]
        provider_row = provider_rows.setdefault(
            provider,
            {
                "provider": provider,
                "services": set(),
                "imported_cost_usd": 0.0,
                "imported_raw_cost_usd": 0.0,
                "imported_estimated_cost_usd": 0.0,
                "usage_lines": 0,
                "session_ids": set(),
            },
        )
        provider_row["services"].add(service)
        provider_row["usage_lines"] += 1
        line_cost = _provider_cost(line)
        provider_row["imported_cost_usd"] += line_cost
        if line.get("raw_cost_usd") is not None:
            provider_row["imported_raw_cost_usd"] += float(line["raw_cost_usd"])
        if line.get("estimated_cost_usd") is not None:
            provider_row["imported_estimated_cost_usd"] += float(line["estimated_cost_usd"])
        session_id = line.get("session_id")
        external_reference = line.get("external_reference")
        if session_id:
            provider_row["session_ids"].add(session_id)
            matched_session_ids.add(session_id)
        if external_reference:
            provider_row["session_ids"].add(external_reference)

    provider_list = []
    for row in provider_rows.values():
        provider_list.append(
            {
                "provider": row["provider"],
                "services": sorted(row["services"]),
                "imported_cost_usd": _round_usd(row["imported_cost_usd"]),
                "imported_raw_cost_usd": _round_usd(row["imported_raw_cost_usd"]),
                "imported_estimated_cost_usd": _round_usd(row["imported_estimated_cost_usd"]),
                "usage_lines": row["usage_lines"],
                "sessions_count": len(row["session_ids"]),
            }
        )
    provider_list.sort(key=lambda item: item["imported_cost_usd"], reverse=True)

    return provider_list, {"matched_session_ids": matched_session_ids}


async def create_reconciliation_run(
    *,
    trigger_mode: str,
    provider_filters: list[str],
    import_ids: list[str],
    period_start: datetime | None,
    period_end: datetime | None,
    created_by: str | None,
) -> str:
    sb = get_supabase_admin()
    result = (
        await sb.table("provider_reconciliation_runs")
        .insert(
            {
                "trigger_mode": trigger_mode,
                "provider_filters": provider_filters,
                "import_ids": import_ids,
                "period_start": _datetime_to_iso(period_start),
                "period_end": _datetime_to_iso(period_end),
                "status": "pending",
                "created_by": created_by,
            }
        )
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    raise RuntimeError("Failed to create reconciliation run")


async def finalize_reconciliation_run(
    run_id: str,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    sb = get_supabase_admin()
    await (
        sb.table("provider_reconciliation_runs")
        .update(
            {
                "status": status,
                "summary": summary or {},
                "error": error,
            }
        )
        .eq("id", run_id)
        .execute()
    )


async def replace_reconciliation_findings(
    run_id: str,
    findings: list[dict[str, Any]],
) -> None:
    sb = get_supabase_admin()
    await sb.table("provider_reconciliation_findings").delete().eq("run_id", run_id).execute()
    if findings:
        payload = [
            {
                "run_id": run_id,
                "severity": finding["severity"],
                "finding_type": finding["finding_type"],
                "provider": finding.get("provider"),
                "service": finding.get("service"),
                "session_id": finding.get("session_id"),
                "user_id": finding.get("user_id"),
                "external_reference": finding.get("external_reference"),
                "expected_cost_usd": finding.get("expected_cost_usd"),
                "actual_cost_usd": finding.get("actual_cost_usd"),
                "delta_cost_usd": finding.get("delta_cost_usd"),
                "metadata": finding.get("metadata") or {},
            }
            for finding in findings
        ]
        await sb.table("provider_reconciliation_findings").insert(payload).execute()


async def list_reconciliation_runs(limit: int = 25) -> list[dict[str, Any]]:
    rows = await _fetch_rows("provider_reconciliation_runs", batch_size=min(limit, 500))
    return rows[:limit]


async def get_reconciliation_run(run_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    run_result = (
        await sb.table("provider_reconciliation_runs").select("*").eq("id", run_id).execute()
    )
    if not run_result.data:
        raise ValueError("Reconciliation run not found")
    findings_result = (
        await sb.table("provider_reconciliation_findings")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at", desc=True)
        .limit(MAX_DETAIL_ROWS)
        .execute()
    )
    return {
        "run": run_result.data[0],
        "findings": findings_result.data or [],
    }


async def run_admin_reconciliation(
    *,
    provider_filters: list[str] | None = None,
    import_ids: list[str] | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    normalized_filters = [_normalize_provider(provider) for provider in (provider_filters or [])]
    run_id = await create_reconciliation_run(
        trigger_mode="manual",
        provider_filters=normalized_filters,
        import_ids=import_ids or [],
        period_start=period_start,
        period_end=period_end,
        created_by=created_by,
    )
    try:
        import_filters: list[tuple[str, str, Any]] = []
        if normalized_filters:
            import_filters.append(("in", "provider", normalized_filters))
        if import_ids:
            import_filters.append(("in", "import_id", import_ids))
        imported_lines = await _fetch_rows(
            "provider_usage_lines",
            filters=import_filters,
        )
        imported_lines = _filter_period_rows(
            imported_lines,
            started_at_key="started_at",
            period_start=period_start,
            period_end=period_end,
        )

        ledger_filters: list[tuple[str, str, Any]] = []
        if period_start:
            ledger_filters.append(("gte", "created_at", _datetime_to_iso(period_start)))
        if period_end:
            ledger_filters.append(("lte", "created_at", _datetime_to_iso(period_end)))
        internal_rows = await _fetch_rows("credit_usage_logs", filters=ledger_filters)
        if normalized_filters:
            internal_rows = [
                row
                for row in internal_rows
                if _infer_internal_provider(row.get("model_id") or "") in normalized_filters
            ]

        imported_provider_breakdown, imported_meta = _summarize_imported_lines(imported_lines)
        internal_provider_breakdown, internal_meta = _summarize_internal_rows(internal_rows)
        imported_by_provider = {row["provider"]: row for row in imported_provider_breakdown}
        internal_by_provider = {row["provider"]: row for row in internal_provider_breakdown}

        provider_summary = []
        findings = list(internal_meta["findings"])
        all_providers = sorted(set(imported_by_provider) | set(internal_by_provider))
        internal_sessions = internal_meta["by_session"]

        for provider in all_providers:
            imported = imported_by_provider.get(provider, {})
            internal = internal_by_provider.get(provider, {})
            imported_cost = float(imported.get("imported_cost_usd") or 0.0)
            internal_provider_cost = float(internal.get("internal_provider_cost_usd") or 0.0)
            internal_billed = float(internal.get("internal_billed_cost_usd") or 0.0)
            delta_cost = internal_provider_cost - imported_cost
            realized_margin = internal_billed - imported_cost
            provider_summary.append(
                {
                    "provider": provider,
                    "imported_cost_usd": _round_usd(imported_cost),
                    "internal_provider_cost_usd": _round_usd(internal_provider_cost),
                    "internal_billed_cost_usd": _round_usd(internal_billed),
                    "delta_provider_cost_usd": _round_usd(delta_cost),
                    "realized_margin_usd": _round_usd(realized_margin),
                    "usage_lines": imported.get("usage_lines", 0),
                    "internal_usage_rows": internal.get("usage_rows", 0),
                }
            )
            if abs(delta_cost) >= 0.01:
                findings.append(
                    {
                        "severity": "warning" if abs(delta_cost) < 1 else "critical",
                        "finding_type": "provider_cost_delta",
                        "provider": provider,
                        "service": None,
                        "session_id": None,
                        "user_id": None,
                        "expected_cost_usd": _round_usd(imported_cost),
                        "actual_cost_usd": _round_usd(internal_provider_cost),
                        "delta_cost_usd": _round_usd(delta_cost),
                        "metadata": {
                            "internal_billed_cost_usd": _round_usd(internal_billed),
                        },
                    }
                )

        known_session_ids = set(internal_sessions.keys())
        for line in imported_lines:
            line_cost = _provider_cost(line)
            session_candidates = {
                value for value in (line.get("session_id"), line.get("external_reference")) if value
            }
            matched_session = next(
                (candidate for candidate in session_candidates if candidate in known_session_ids),
                None,
            )
            if session_candidates and not matched_session:
                findings.append(
                    {
                        "severity": "warning",
                        "finding_type": "unmatched_provider_line",
                        "provider": line.get("provider"),
                        "service": line.get("service"),
                        "session_id": line.get("session_id"),
                        "user_id": line.get("user_id"),
                        "external_reference": line.get("external_reference"),
                        "expected_cost_usd": _round_usd(line_cost),
                        "actual_cost_usd": 0.0,
                        "delta_cost_usd": _round_usd(-line_cost),
                        "metadata": {
                            "resource_id": line.get("resource_id"),
                            "import_id": line.get("import_id"),
                        },
                    }
                )
            if matched_session:
                session_summary = internal_sessions[matched_session]
                if session_summary["billed_cost_usd"] < line_cost:
                    findings.append(
                        {
                            "severity": "critical",
                            "finding_type": "negative_margin_session",
                            "provider": line.get("provider"),
                            "service": line.get("service"),
                            "session_id": matched_session,
                            "user_id": line.get("user_id"),
                            "external_reference": line.get("external_reference"),
                            "expected_cost_usd": _round_usd(line_cost),
                            "actual_cost_usd": _round_usd(session_summary["billed_cost_usd"]),
                            "delta_cost_usd": _round_usd(
                                session_summary["billed_cost_usd"] - line_cost
                            ),
                            "metadata": {
                                "internal_provider_cost_usd": _round_usd(
                                    session_summary["provider_cost_usd"]
                                ),
                            },
                        }
                    )

        summary = {
            "providers": provider_summary,
            "totals": {
                "imported_cost_usd": _round_usd(
                    sum(item["imported_cost_usd"] for item in provider_summary)
                ),
                "internal_provider_cost_usd": _round_usd(
                    sum(item["internal_provider_cost_usd"] for item in provider_summary)
                ),
                "internal_billed_cost_usd": _round_usd(
                    sum(item["internal_billed_cost_usd"] for item in provider_summary)
                ),
                "realized_margin_usd": _round_usd(
                    sum(item["realized_margin_usd"] for item in provider_summary)
                ),
            },
            "counts": {
                "imported_usage_lines": len(imported_lines),
                "internal_usage_rows": len(internal_rows),
                "findings": len(findings),
                "matched_import_session_ids": len(imported_meta["matched_session_ids"]),
            },
        }
        await replace_reconciliation_findings(run_id, findings)
        await finalize_reconciliation_run(run_id, status="completed", summary=summary)
        return {
            "run_id": run_id,
            "summary": summary,
            "findings": findings,
        }
    except Exception as exc:
        await finalize_reconciliation_run(run_id, status="failed", error=str(exc))
        raise
