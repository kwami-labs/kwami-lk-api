"""`/brain`: the work the brain does, asked from the app, on the PC or in the cloud."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services import brain_service

router = APIRouter()

NOT_READY = "The brain's cloud is not set up on this server yet"


class CloudSetting(BaseModel):
    enabled: bool


class JobRequest(BaseModel):
    kwami_id: str
    task: str = Field(..., min_length=1, max_length=brain_service.MAX_TASK_CHARS)
    title: str = Field("", max_length=brain_service.MAX_TITLE_CHARS)
    target: Literal["auto", "pc", "cloud"] = "auto"
    difficulty: Literal["simple", "normal", "hard"] = "normal"
    agent_id: UUID | None = None


class AgentFields(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    effort: str | None = None
    target: str | None = None
    enabled: bool | None = None

    def given(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class AgentRun(BaseModel):
    kwami_id: str
    task: str = Field("", max_length=brain_service.MAX_TASK_CHARS)


def _not_ready() -> HTTPException:
    return HTTPException(status_code=503, detail=NOT_READY)


@router.get("/where")
async def read_where(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    try:
        return await brain_service.where(user.id)
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc


@router.put("/cloud")
async def write_cloud(
    body: CloudSetting, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    try:
        return {"enabled": await brain_service.set_cloud(user.id, body.enabled)}
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc


@router.get("/jobs")
async def read_jobs(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=brain_service.MAX_LIST)] = 20,
) -> dict[str, Any]:
    try:
        return {"jobs": await brain_service.list_jobs(user.id, kwami_id, limit)}
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc


@router.post("/jobs")
async def ask_for_work(
    body: JobRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return await brain_service.queue_job(
            user.id,
            body.kwami_id,
            body.task,
            title=body.title,
            target=body.target,
            difficulty=body.difficulty,
            agent_id=str(body.agent_id) if body.agent_id else None,
        )
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    except brain_service.AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such agent") from exc
    except brain_service.TooManyJobsError as exc:
        raise HTTPException(status_code=429, detail="Too much work waiting already") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    try:
        cancelled = await brain_service.cancel_job(user.id, str(job_id))
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    if not cancelled:
        raise HTTPException(status_code=404, detail="Nothing to cancel")
    return {"cancelled": True}


@router.get("/agents")
async def read_agents(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    try:
        return {
            "agents": await brain_service.list_agents(user.id),
            "models": list(brain_service.AGENT_MODELS),
            "tools": list(brain_service.AGENT_TOOLS),
        }
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc


@router.post("/agents", status_code=201)
async def create_agent(
    body: AgentFields, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return await brain_service.create_agent(user.id, body.given())
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: UUID, body: AgentFields, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return await brain_service.update_agent(user.id, str(agent_id), body.given())
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    except brain_service.AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such agent") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]) -> None:
    try:
        await brain_service.delete_agent(user.id, str(agent_id))
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    except brain_service.AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such agent") from exc


@router.post("/agents/{agent_id}/run")
async def run_agent(
    agent_id: UUID, body: AgentRun, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return await brain_service.run_agent(user.id, str(agent_id), body.kwami_id, body.task)
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    except brain_service.AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such agent") from exc
    except brain_service.TooManyJobsError as exc:
        raise HTTPException(status_code=429, detail="Too much work waiting already") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sync")
async def read_sync(
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: Annotated[int, Query(ge=1, le=brain_service.MAX_LIST)] = 20,
) -> dict[str, Any]:
    try:
        return {"items": await brain_service.list_sync(user.id, limit)}
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc


@router.post("/sync/{sync_id}/discard")
async def discard_sync(
    sync_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    try:
        discarded = await brain_service.discard_sync(user.id, str(sync_id))
    except brain_service.BrainNotReadyError as exc:
        raise _not_ready() from exc
    if not discarded:
        raise HTTPException(status_code=404, detail="Nothing waiting to discard")
    return {"discarded": True}
