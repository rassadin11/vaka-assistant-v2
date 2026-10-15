"""Topic notes endpoints for the Telegram Mini App."""

# ruff: noqa: RUF001

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, cast
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, FastAPI, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.notes_service import (
    MAX_NOTE_CONTENT_LENGTH,
    InvalidNoteTitle,
    NoteContentTooLong,
    NoteNotFound,
    NoteRecord,
    NotesConnection,
    NotesLimitReached,
    NoteStale,
    NoteTitleTaken,
    create_note,
    delete_note_by_id,
    get_note,
    list_note_summaries,
    notes_limit,
    sanitize_title,
    update_note,
)
from core.rate_limit import RateLimitRedis
from core.tracing import current_trace_id
from webapp.dependencies import active_request_user, bearer_subject, require_webapp_rate_limit
from webapp.errors import WebAppError
from webapp.metrics import WebAppMetrics

PoolGetter = Callable[[], asyncpg.Pool]
CacheGetter = Callable[[], RateLimitRedis]
LOGGER = logging.getLogger(__name__)

CONTENT_TOO_LONG_MESSAGE = (
    f"Текст заметки слишком длинный: допустимо не более {MAX_NOTE_CONTENT_LENGTH} символов."
)
TITLE_TAKEN_MESSAGE = "Заметка с таким заголовком уже есть."
STALE_MESSAGE = "Заметка изменилась, пока вы её правили."


class NoteListItem(BaseModel):
    """One row of the notes list."""

    id: int
    title: str
    chars: int
    updated_at: str


class NotesListResponse(BaseModel):
    """All notes of the user plus the profile limit."""

    notes: list[NoteListItem]
    notes_total: int
    notes_limit: int


class NoteResponse(BaseModel):
    """One full note."""

    id: int
    title: str
    content: str
    chars: int
    created_at: str
    updated_at: str


class CreateNoteRequest(BaseModel):
    """Transport shape of a new note; limits are enforced by the domain service."""

    model_config = ConfigDict(extra="forbid")

    title: str
    content: str = ""


class UpdateNoteRequest(BaseModel):
    """Partial note update with an optional optimistic version check."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = None
    expected_updated_at: AwareDatetime | None = None


def install_notes_routes(
    app: FastAPI,
    *,
    pool: PoolGetter,
    cache: CacheGetter,
    session_secret: str,
    metrics: WebAppMetrics,
) -> None:
    """Attach note routes with injectable runtime dependencies."""

    router = APIRouter(prefix="/app/api/notes")

    @router.get("", response_model=NotesListResponse)
    async def list_all(
        authorization: Annotated[str | None, Header()] = None,
    ) -> NotesListResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            items = await list_note_summaries(cast(NotesConnection, user.connection))
        return NotesListResponse(
            notes=[
                NoteListItem(
                    id=item.id,
                    title=item.title,
                    chars=item.chars,
                    updated_at=item.updated_at.astimezone(timezone).isoformat(),
                )
                for item in items
            ],
            notes_total=len(items),
            notes_limit=notes_limit(),
        )

    @router.get("/{note_id}", response_model=NoteResponse)
    async def read_one(
        note_id: int,
        authorization: Annotated[str | None, Header()] = None,
    ) -> NoteResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            note = await get_note(cast(NotesConnection, user.connection), note_id)
        if note is None:
            raise _not_found()
        return _note_response(note, timezone)

    @router.post("", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
    async def create(
        payload: CreateNoteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> NoteResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            try:
                note = await create_note(
                    cast(NotesConnection, user.connection),
                    user_id,
                    sanitize_title(payload.title),
                    payload.content,
                )
            except (InvalidNoteTitle, NoteContentTooLong, NoteTitleTaken, NotesLimitReached) as exc:
                raise _domain_error(exc) from exc
        metrics.notes_created.inc()
        LOGGER.info("Mini App note created")
        return _note_response(note, timezone)

    @router.patch("/{note_id}", response_model=NoteResponse)
    async def update(
        note_id: int,
        payload: UpdateNoteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        if payload.title is None and payload.content is None:
            raise WebAppError(400, "nothing_to_update", "Нужно указать заголовок или текст.")
        stale: NoteRecord | None = None
        note: NoteRecord | None = None
        async with active_request_user(pool(), user_id) as user:
            timezone = ZoneInfo(user.timezone)
            try:
                note = await update_note(
                    cast(NotesConnection, user.connection),
                    note_id,
                    title=sanitize_title(payload.title) if payload.title is not None else None,
                    content=payload.content,
                    expected_updated_at=payload.expected_updated_at,
                )
            except NoteStale as exc:
                stale = exc.current
            except NoteNotFound as exc:
                raise _not_found() from exc
            except (InvalidNoteTitle, NoteContentTooLong, NoteTitleTaken) as exc:
                raise _domain_error(exc) from exc
        if stale is not None:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "stale",
                        "message": STALE_MESSAGE,
                        "trace_id": current_trace_id() or "-",
                    },
                    "note": _note_response(stale, timezone).model_dump(),
                },
            )
        if note is None:
            raise RuntimeError("note update produced no result")
        metrics.notes_updated.inc()
        LOGGER.info("Mini App note updated")
        return JSONResponse(content=_note_response(note, timezone).model_dump())

    @router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove(
        note_id: int,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            deleted = await delete_note_by_id(cast(NotesConnection, user.connection), note_id)
        if not deleted:
            raise _not_found()
        metrics.notes_deleted.inc()
        LOGGER.info("Mini App note deleted")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)


def _not_found() -> WebAppError:
    return WebAppError(404, "note_not_found", "Заметка не найдена.")


def _note_response(note: NoteRecord, timezone: ZoneInfo) -> NoteResponse:
    return NoteResponse(
        id=note.id,
        title=note.title,
        content=note.content,
        chars=len(note.content),
        created_at=note.created_at.astimezone(timezone).isoformat(),
        updated_at=note.updated_at.astimezone(timezone).isoformat(),
    )


def _domain_error(exc: Exception) -> WebAppError:
    if isinstance(exc, InvalidNoteTitle):
        return WebAppError(400, "invalid_title", exc.message)
    if isinstance(exc, NoteContentTooLong):
        return WebAppError(400, "content_too_long", CONTENT_TOO_LONG_MESSAGE)
    if isinstance(exc, NoteTitleTaken):
        return WebAppError(409, "title_taken", TITLE_TAKEN_MESSAGE)
    if isinstance(exc, NotesLimitReached):
        return WebAppError(
            409,
            "notes_limit",
            f"Достигнут лимит заметок: {exc.limit}. Удалите ненужные, чтобы создать новую.",
        )
    raise exc
