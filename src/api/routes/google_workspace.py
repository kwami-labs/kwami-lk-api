"""Drive, Docs, Sheets, Slides, Contacts and Tasks of the user's linked Google accounts.

Sharing a file speaks to someone in the user's name, so the app puts it through the
same gate as an email before it gets here.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.routes.google import Account, _call
from src.core.security import AuthUser
from src.services import google_workspace as workspace

router = APIRouter()

User = Annotated[AuthUser, Depends(require_auth)]
Name = Field(..., min_length=1, max_length=500)


class FileCreateRequest(BaseModel):
    name: str = Name
    content: str = ""
    mime_type: str = Field(default="text/plain", max_length=200)
    parent_id: str | None = None


class FileWriteRequest(BaseModel):
    content: str
    mime_type: str = Field(default="text/plain", max_length=200)


class FolderCreateRequest(BaseModel):
    name: str = Name
    parent_id: str | None = None


class FileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    add_parent: str | None = None
    remove_parent: str | None = None


class FileCopyRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)


class ShareRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    role: str = "reader"


class AttachmentSaveRequest(BaseModel):
    filename: str = Name
    mime_type: str = Field(default="application/octet-stream", max_length=200)
    parent_id: str | None = None


class DocumentCreateRequest(BaseModel):
    title: str = Name
    text: str = ""


class DocumentAppendRequest(BaseModel):
    text: str = Field(..., min_length=1)


class DocumentReplaceRequest(BaseModel):
    find: str = Field(..., min_length=1)
    replace: str


class TitleRequest(BaseModel):
    title: str = Name


class CellsRequest(BaseModel):
    range: str = Field(..., min_length=1, max_length=200)
    values: list[list[Any]] = Field(..., min_length=1)


class SlideRequest(BaseModel):
    title: str = Name
    body: str = ""


class ContactCreateRequest(BaseModel):
    name: str = Name
    email: str | None = None
    phone: str | None = None
    organization: str | None = None


class ContactUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    email: str | None = None
    phone: str | None = None
    organization: str | None = None


class TaskCreateRequest(BaseModel):
    title: str = Name
    notes: str | None = None
    due: str | None = None
    task_list: str = "@default"


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = None
    due: str | None = None
    done: bool | None = None
    task_list: str = "@default"


# -- Drive ----------------------------------------------------------------------


@router.get("/drive/files")
async def drive_search(
    user: User,
    q: str = Query(default=""),
    max_results: int = Query(default=20, ge=1, le=50),
    account: Account = None,
) -> dict[str, Any]:
    return {"files": await _call(workspace.search_files(user.id, q, max_results, account=account))}


@router.get("/drive/files/{file_id}")
async def drive_read(file_id: str, user: User, account: Account = None) -> dict[str, Any]:
    return {"file": await _call(workspace.read_file(user.id, file_id, account=account))}


@router.post("/drive/files")
async def drive_create(
    body: FileCreateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    return {
        "file": await _call(workspace.create_file(user.id, account=account, **body.model_dump()))
    }


@router.put("/drive/files/{file_id}/content")
async def drive_write(
    file_id: str, body: FileWriteRequest, user: User, account: Account = None
) -> dict[str, Any]:
    written = await _call(
        workspace.write_file(user.id, file_id, account=account, **body.model_dump())
    )
    return {"file": written}


@router.post("/drive/folders")
async def drive_folder(
    body: FolderCreateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    created = await _call(workspace.create_folder(user.id, account=account, **body.model_dump()))
    return {"file": created}


@router.patch("/drive/files/{file_id}")
async def drive_update(
    file_id: str, body: FileUpdateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    updated = await _call(
        workspace.update_file(user.id, file_id, account=account, **body.model_dump())
    )
    return {"file": updated}


@router.post("/drive/files/{file_id}/copy")
async def drive_copy(
    file_id: str, body: FileCopyRequest, user: User, account: Account = None
) -> dict[str, Any]:
    copied = await _call(workspace.copy_file(user.id, file_id, name=body.name, account=account))
    return {"file": copied}


@router.post("/drive/files/{file_id}/trash")
async def drive_trash(file_id: str, user: User, account: Account = None) -> dict[str, Any]:
    return {"file": await _call(workspace.set_trashed(user.id, file_id, True, account=account))}


@router.post("/drive/files/{file_id}/untrash")
async def drive_untrash(file_id: str, user: User, account: Account = None) -> dict[str, Any]:
    return {"file": await _call(workspace.set_trashed(user.id, file_id, False, account=account))}


@router.get("/drive/files/{file_id}/permissions")
async def drive_permissions(file_id: str, user: User, account: Account = None) -> dict[str, Any]:
    found = await _call(workspace.list_permissions(user.id, file_id, account=account))
    return {"permissions": found}


@router.post("/drive/files/{file_id}/permissions")
async def drive_share(
    file_id: str, body: ShareRequest, user: User, account: Account = None
) -> dict[str, Any]:
    shared = await _call(
        workspace.share_file(user.id, file_id, email=body.email, role=body.role, account=account)
    )
    return {"permission": shared}


@router.delete("/drive/files/{file_id}/permissions/{permission_id}")
async def drive_unshare(
    file_id: str, permission_id: str, user: User, account: Account = None
) -> dict[str, bool]:
    await _call(workspace.unshare_file(user.id, file_id, permission_id, account=account))
    return {"ok": True}


@router.post("/gmail/messages/{message_id}/attachments/{attachment_id}/save")
async def gmail_attachment_save(
    message_id: str,
    attachment_id: str,
    body: AttachmentSaveRequest,
    user: User,
    account: Account = None,
) -> dict[str, Any]:
    saved = await _call(
        workspace.save_attachment(
            user.id, message_id, attachment_id, account=account, **body.model_dump()
        )
    )
    return {"file": saved}


# -- Docs -----------------------------------------------------------------------


@router.post("/docs")
async def docs_create(
    body: DocumentCreateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    created = await _call(
        workspace.create_document(user.id, title=body.title, text=body.text, account=account)
    )
    return {"document": created}


@router.post("/docs/{document_id}/append")
async def docs_append(
    document_id: str, body: DocumentAppendRequest, user: User, account: Account = None
) -> dict[str, bool]:
    await _call(workspace.append_to_document(user.id, document_id, text=body.text, account=account))
    return {"ok": True}


@router.post("/docs/{document_id}/replace")
async def docs_replace(
    document_id: str, body: DocumentReplaceRequest, user: User, account: Account = None
) -> dict[str, int]:
    changed = await _call(
        workspace.replace_in_document(
            user.id, document_id, find=body.find, replace=body.replace, account=account
        )
    )
    return {"changed": changed}


# -- Sheets ---------------------------------------------------------------------


@router.post("/sheets")
async def sheets_create(body: TitleRequest, user: User, account: Account = None) -> dict[str, Any]:
    created = await _call(workspace.create_spreadsheet(user.id, title=body.title, account=account))
    return {"spreadsheet": created}


@router.get("/sheets/{spreadsheet_id}/values")
async def sheets_read(
    spreadsheet_id: str,
    user: User,
    cell_range: Annotated[str, Query(alias="range", min_length=1, max_length=200)],
    account: Account = None,
) -> dict[str, Any]:
    values = await _call(workspace.read_range(user.id, spreadsheet_id, cell_range, account=account))
    return {"values": values}


@router.put("/sheets/{spreadsheet_id}/values")
async def sheets_write(
    spreadsheet_id: str, body: CellsRequest, user: User, account: Account = None
) -> dict[str, int]:
    changed = await _call(
        workspace.write_range(
            user.id, spreadsheet_id, body.range, values=body.values, account=account
        )
    )
    return {"changed": changed}


@router.post("/sheets/{spreadsheet_id}/values")
async def sheets_append(
    spreadsheet_id: str, body: CellsRequest, user: User, account: Account = None
) -> dict[str, bool]:
    await _call(
        workspace.append_rows(
            user.id, spreadsheet_id, body.range, values=body.values, account=account
        )
    )
    return {"ok": True}


# -- Slides ---------------------------------------------------------------------


@router.post("/slides")
async def slides_create(body: TitleRequest, user: User, account: Account = None) -> dict[str, Any]:
    created = await _call(workspace.create_presentation(user.id, title=body.title, account=account))
    return {"presentation": created}


@router.post("/slides/{presentation_id}/slides")
async def slides_add(
    presentation_id: str, body: SlideRequest, user: User, account: Account = None
) -> dict[str, str]:
    slide_id = await _call(
        workspace.add_slide(
            user.id, presentation_id, title=body.title, body=body.body, account=account
        )
    )
    return {"id": slide_id}


# -- Contacts -------------------------------------------------------------------


@router.get("/contacts")
async def contacts_search(
    user: User, q: str = Query(default=""), account: Account = None
) -> dict[str, Any]:
    return {"contacts": await _call(workspace.search_contacts(user.id, q, account=account))}


@router.post("/contacts")
async def contacts_create(
    body: ContactCreateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    created = await _call(workspace.create_contact(user.id, account=account, **body.model_dump()))
    return {"contact": created}


@router.patch("/contacts/{resource_name:path}")
async def contacts_update(
    resource_name: str, body: ContactUpdateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    updated = await _call(
        workspace.update_contact(user.id, resource_name, account=account, **body.model_dump())
    )
    return {"contact": updated}


# -- Tasks ----------------------------------------------------------------------


@router.get("/tasks/lists")
async def tasks_lists(user: User, account: Account = None) -> dict[str, Any]:
    return {"lists": await _call(workspace.list_task_lists(user.id, account=account))}


@router.get("/tasks")
async def tasks_list(
    user: User,
    task_list: str = Query(default="@default"),
    show_done: bool = Query(default=False),
    account: Account = None,
) -> dict[str, Any]:
    found = await _call(
        workspace.list_tasks(user.id, task_list, show_done=show_done, account=account)
    )
    return {"tasks": found}


@router.post("/tasks")
async def tasks_create(
    body: TaskCreateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    created = await _call(workspace.create_task(user.id, account=account, **body.model_dump()))
    return {"task": created}


@router.patch("/tasks/{task_id}")
async def tasks_update(
    task_id: str, body: TaskUpdateRequest, user: User, account: Account = None
) -> dict[str, Any]:
    updated = await _call(
        workspace.update_task(user.id, task_id, account=account, **body.model_dump())
    )
    return {"task": updated}
