"""What the work thread on the user's PC may do in their Google accounts.

An allow list, because that process runs with no one watching and reads mail and files
that can say anything. It reads, organizes, drafts and creates. Nothing here speaks to
anyone: sending, inviting and sharing need the app's check, and a filter is a standing
rule the user would not see being made.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.services import google_service as mail
from src.services import google_workspace as workspace

ALLOWED: dict[str, Callable[..., Awaitable[Any]]] = {
    "accounts": mail.connection_status,
    "gmail_search": mail.list_messages,
    "gmail_read": mail.read_message,
    "gmail_attachment": mail.read_attachment,
    "gmail_save_attachment": workspace.save_attachment,
    "gmail_labels": mail.list_labels,
    "gmail_create_label": mail.create_label,
    "gmail_label": mail.modify_labels,
    "gmail_archive": mail.archive_message,
    "gmail_trash": mail.trash_message,
    "gmail_untrash": mail.untrash_message,
    "gmail_drafts": mail.list_drafts,
    "gmail_draft": mail.create_draft,
    "gmail_filters": mail.list_filters,
    "calendar_list": mail.list_events,
    "calendar_create": mail.create_event,
    "calendar_update": mail.update_event,
    "calendar_delete": mail.delete_event,
    "drive_search": workspace.search_files,
    "drive_read": workspace.read_file,
    "drive_create": workspace.create_file,
    "drive_write": workspace.write_file,
    "drive_folder": workspace.create_folder,
    "drive_update": workspace.update_file,
    "drive_copy": workspace.copy_file,
    "drive_trash": workspace.set_trashed,
    "drive_permissions": workspace.list_permissions,
    "docs_create": workspace.create_document,
    "docs_append": workspace.append_to_document,
    "docs_replace": workspace.replace_in_document,
    "sheets_create": workspace.create_spreadsheet,
    "sheets_read": workspace.read_range,
    "sheets_write": workspace.write_range,
    "sheets_append": workspace.append_rows,
    "slides_create": workspace.create_presentation,
    "slides_add": workspace.add_slide,
    "contacts_search": workspace.search_contacts,
    "contacts_create": workspace.create_contact,
    "contacts_update": workspace.update_contact,
    "tasks_lists": workspace.list_task_lists,
    "tasks_list": workspace.list_tasks,
    "tasks_create": workspace.create_task,
    "tasks_update": workspace.update_task,
}


async def call(user_id: str, action: str, args: dict[str, Any]) -> Any:
    """Run one allowed action. Anything else raises ValueError."""
    function = ALLOWED.get(action)
    if function is None:
        raise ValueError(f"Not allowed from the work thread: {action}")
    if action in ("calendar_create", "calendar_update") and args.get("attendees"):
        raise ValueError("Inviting people is not allowed from the work thread")
    if "user_id" in args:
        raise ValueError("user_id cannot be passed")
    try:
        return await function(user_id, **args)
    except TypeError as exc:
        raise ValueError(f"Wrong arguments for {action}") from exc
