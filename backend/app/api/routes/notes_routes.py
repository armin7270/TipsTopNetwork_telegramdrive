"""API routes for notes and daily notes in TeleDrive."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.services.auth import Principal, current_user

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteCreateRequest(BaseModel):
    title: str = Field(default="یادداشت جدید", max_length=255)
    content: str = Field(default="")
    date_shamsi: str | None = Field(default=None, max_length=20)
    is_daily: bool = Field(default=False)
    color: str | None = Field(default="#38bdf8", max_length=50)
    is_pinned: bool = Field(default=False)
    checklist: list[dict[str, Any]] | None = Field(default=None)
    tags: list[str] | None = Field(default=None)
    reminder_at: str | None = Field(default=None, max_length=50)


class NoteUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None)
    date_shamsi: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=50)
    is_pinned: bool | None = Field(default=None)
    checklist: list[dict[str, Any]] | None = Field(default=None)
    tags: list[str] | None = Field(default=None)
    reminder_at: str | None = Field(default=None, max_length=50)


@router.get("", summary="List notes")
async def list_notes(
    request: Request,
    principal: Principal = Depends(current_user),
    date_shamsi: str | None = Query(default=None),
    is_daily: bool | None = Query(default=None),
) -> list[dict[str, Any]]:
    repo = request.app.state.repo
    return await repo.list_notes(
        owner_id=principal.user_id,
        date_shamsi=date_shamsi,
        is_daily=is_daily,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a note")
async def create_note(
    payload: NoteCreateRequest,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    return await repo.create_note(
        owner_id=principal.user_id,
        title=payload.title,
        content=payload.content,
        date_shamsi=payload.date_shamsi,
        is_daily=payload.is_daily,
        color=payload.color,
        is_pinned=payload.is_pinned,
        checklist=payload.checklist,
        tags=payload.tags,
        reminder_at=payload.reminder_at,
    )


@router.get("/{note_id}", summary="Get a note")
async def get_note(
    note_id: str,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    note = await repo.get_note(note_id, owner_id=principal.user_id)
    if not note:
        raise HTTPException(status_code=404, detail="یادداشت یافت نشد")
    return note


@router.patch("/{note_id}", summary="Update a note")
async def update_note(
    note_id: str,
    payload: NoteUpdateRequest,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    updated = await repo.update_note(
        note_id,
        owner_id=principal.user_id,
        title=payload.title,
        content=payload.content,
        date_shamsi=payload.date_shamsi,
        color=payload.color,
        is_pinned=payload.is_pinned,
        checklist=payload.checklist,
        tags=payload.tags,
        reminder_at=payload.reminder_at,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="یادداشت یافت نشد")
    return updated


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a note")
async def delete_note(
    note_id: str,
    request: Request,
    principal: Principal = Depends(current_user),
) -> None:
    repo = request.app.state.repo
    ok = await repo.delete_note(note_id, owner_id=principal.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="یادداشت یافت نشد")
