"""Centralized bot authorization helpers for resource ownership checks."""

from __future__ import annotations

from pathlib import Path

from .storage import EditJob, JobRecord, get_edit_job, get_job

NOT_FOUND_OR_UNAUTHORIZED = "Not found or not authorized."


class ResourceNotFound(Exception):
    """Raised when a resource is missing or not owned by the principal."""


async def require_owned_job(
    db_path: Path, job_id: int, principal_user_id: int
) -> JobRecord:
    job = await get_job(db_path, job_id)
    if job is None or job.user_id != principal_user_id:
        raise ResourceNotFound
    return job


async def require_owned_edit(
    db_path: Path, edit_id: int, principal_user_id: int
) -> EditJob:
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.user_id != principal_user_id:
        raise ResourceNotFound
    return edit
