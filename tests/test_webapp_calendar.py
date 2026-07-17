"""HTTP coverage for Mini App calendar mutations and RLS boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from prometheus_client import CollectorRegistry

from webapp.app import create_app
from webapp.auth import create_session_token
from webapp.metrics import WebAppMetrics
from webapp.settings import WebAppSettings

SESSION_SECRET = "calendar-test-session-secret"
NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)


class CalendarConnection:
    def __init__(self, users: dict[UUID, dict[str, str]], tasks: list[dict[str, Any]]) -> None:
        self.users = users
        self.tasks = tasks
        self.current_user_id: UUID | None = None
        self.next_id = max((int(task["id"]) for task in tasks), default=0) + 1

    async def execute(self, query: str, *args: object) -> str:
        if "set_config('app.user_id'" in query:
            self.current_user_id = UUID(str(args[0]))
            return "SELECT 1"
        if "SET status = 'cancelled'" in query:
            task = self._visible_task(int(args[0]))
            if task is None:
                return "UPDATE 0"
            task["status"] = "cancelled"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {query}")

    async def fetchval(self, query: str, *args: object) -> object:
        if query.strip() == "SELECT 1":
            return 1
        if "COUNT(*)" in query and "scheduled_tasks" in query:
            return sum(
                task["user_id"] == self.current_user_id
                and task["kind"] == "reminder"
                and task["status"] == "active"
                for task in self.tasks
            )
        if "INSERT INTO scheduled_tasks" in query:
            task = {
                "id": self.next_id,
                "user_id": args[0],
                "kind": "reminder",
                "title": args[1],
                "payload": args[2],
                "cron_expr": args[3],
                "next_run_at": args[4],
                "status": "active",
            }
            self.tasks.append(task)
            self.next_id += 1
            return task["id"]
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "FROM users" in query:
            user = self.users.get(UUID(str(args[0])))
            if user is None or UUID(str(args[0])) != self.current_user_id:
                return None
            return user.copy()
        if "SELECT kind, status" in query:
            task = self._visible_task(int(args[0]))
            return None if task is None else {"kind": task["kind"], "status": task["status"]}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        if "FROM scheduled_tasks" not in query:
            raise AssertionError(f"unexpected fetch: {query}")
        start, end = args
        assert isinstance(start, datetime) and isinstance(end, datetime)
        return [
            task.copy()
            for task in self.tasks
            if task["user_id"] == self.current_user_id
            and (
                task["status"] == "active"
                or (task["status"] == "done" and start <= task["next_run_at"] < end)
            )
        ]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield

    def _visible_task(self, task_id: int) -> dict[str, Any] | None:
        return next(
            (
                task
                for task in self.tasks
                if task["id"] == task_id and task["user_id"] == self.current_user_id
            ),
            None,
        )


class CalendarPool:
    def __init__(self, users: dict[UUID, dict[str, str]], tasks: list[dict[str, Any]]) -> None:
        self.connection = CalendarConnection(users, tasks)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[CalendarConnection]:
        yield self.connection


class AllowRedis:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        return [1, 0]

    async def ping(self) -> object:
        return True

    async def aclose(self) -> None:
        return None


def _settings() -> WebAppSettings:
    return WebAppSettings(
        telegram_bot_token="calendar-test-bot",
        session_secret=SESSION_SECRET,
        database_url="postgresql://unused",
        redis_cache_url="redis://unused",
    )


def _task(
    task_id: int,
    user_id: UUID,
    status: str = "active",
    *,
    kind: str = "reminder",
    run_at: datetime | None = None,
    cron: str | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "user_id": user_id,
        "kind": kind,
        "title": f"Task {task_id}",
        "payload": f"Reminder {task_id}",
        "cron_expr": cron,
        "next_run_at": run_at or datetime(2026, 7, 18, 7, tzinfo=UTC),
        "status": status,
    }


def _client(
    users: dict[UUID, dict[str, str]], tasks: list[dict[str, Any]]
) -> tuple[httpx.AsyncClient, CalendarPool]:
    pool = CalendarPool(users, tasks)
    app = create_app(
        settings=_settings(),
        pool=pool,  # type: ignore[arg-type]
        cache_redis=AllowRedis(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        metrics=WebAppMetrics(CollectorRegistry()),
    )
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://calendar.test"),
        pool,
    )


def _users(*user_ids: UUID) -> dict[UUID, dict[str, str]]:
    return {
        user_id: {"status": "active", "timezone": "Europe/Moscow", "plan": "trial"}
        for user_id in user_ids
    }


def _auth(user_id: UUID) -> dict[str, str]:
    token = create_session_token(user_id, SESSION_SECRET)
    return {"Authorization": f"Bearer {token}"}


async def test_every_calendar_endpoint_requires_authentication() -> None:
    user_id = uuid4()
    client, _pool = _client(_users(user_id), [])
    async with client:
        calendar = await client.get("/app/api/calendar?from=2026-07-01&to=2026-07-31")
        created = await client.post(
            "/app/api/reminders",
            json={"text": "Test", "remind_at_local": "2026-07-18T10:00"},
        )
        cancelled = await client.delete("/app/api/scheduled/1")

    assert [calendar.status_code, created.status_code, cancelled.status_code] == [401, 401, 401]


async def test_calendar_is_rls_isolated_and_foreign_cancel_is_404() -> None:
    user_a, user_b = uuid4(), uuid4()
    tasks = [_task(1, user_a), _task(2, user_b)]
    client, _pool = _client(_users(user_a, user_b), tasks)
    async with client:
        calendar_a = await client.get(
            "/app/api/calendar?from=2026-07-01&to=2026-07-31", headers=_auth(user_a)
        )
        foreign_cancel = await client.delete("/app/api/scheduled/2", headers=_auth(user_a))

    items = [item for day in calendar_a.json()["days"].values() for item in day]
    assert [item["id"] for item in items] == [1]
    assert foreign_cancel.status_code == 404
    assert tasks[1]["status"] == "active"


async def test_create_appears_then_cancel_disappears() -> None:
    user_id = uuid4()
    tasks: list[dict[str, Any]] = []
    client, _pool = _client(_users(user_id), tasks)
    async with client:
        created = await client.post(
            "/app/api/reminders",
            headers=_auth(user_id),
            json={"text": "Позвонить", "remind_at_local": "2026-07-18T10:00"},
        )
        before = await client.get(
            "/app/api/calendar?from=2026-07-18&to=2026-07-18", headers=_auth(user_id)
        )
        cancelled = await client.delete(
            f"/app/api/scheduled/{created.json()['id']}", headers=_auth(user_id)
        )
        after = await client.get(
            "/app/api/calendar?from=2026-07-18&to=2026-07-18", headers=_auth(user_id)
        )

    assert created.status_code == 201
    assert before.json()["days"]["2026-07-18"][0]["text"] == "Позвонить"
    assert cancelled.status_code == 200
    assert after.json() == {"days": {}}


async def test_done_is_visible_cancelled_hidden_and_terminal_cancel_conflicts() -> None:
    user_id = uuid4()
    tasks = [
        _task(1, user_id, "done"),
        _task(2, user_id, "cancelled"),
        _task(3, user_id, "done", kind="agent_task"),
    ]
    client, _pool = _client(_users(user_id), tasks)
    async with client:
        calendar = await client.get(
            "/app/api/calendar?from=2026-07-18&to=2026-07-18", headers=_auth(user_id)
        )
        done_conflict = await client.delete("/app/api/scheduled/1", headers=_auth(user_id))
        cancelled_conflict = await client.delete("/app/api/scheduled/2", headers=_auth(user_id))
        missing = await client.delete("/app/api/scheduled/999", headers=_auth(user_id))

    ids = [item["id"] for item in calendar.json()["days"]["2026-07-18"]]
    assert ids == [1, 3]
    assert done_conflict.status_code == cancelled_conflict.status_code == 409
    assert missing.status_code == 404


async def test_create_transport_domain_limits_and_calendar_range() -> None:
    user_id = uuid4()
    tasks = [_task(index + 1, user_id) for index in range(25)]
    client, _pool = _client(_users(user_id), tasks)
    async with client:
        empty = await client.post(
            "/app/api/reminders",
            headers=_auth(user_id),
            json={"text": "", "remind_at_local": "2026-07-18T10:00"},
        )
        too_long = await client.post(
            "/app/api/reminders",
            headers=_auth(user_id),
            json={"text": "x" * 501, "remind_at_local": "2026-07-18T10:00"},
        )
        past = await client.post(
            "/app/api/reminders",
            headers=_auth(user_id),
            json={"text": "Прошлое", "remind_at_local": "2026-07-17T11:59"},
        )
        limited = await client.post(
            "/app/api/reminders",
            headers=_auth(user_id),
            json={"text": "Ещё", "remind_at_local": "2026-07-18T10:00"},
        )
        range_error = await client.get(
            "/app/api/calendar?from=2026-07-01&to=2026-09-01", headers=_auth(user_id)
        )

    assert empty.status_code == too_long.status_code == 422
    assert past.status_code == 422
    assert past.json()["error"]["message"] == "Это время уже прошло"
    assert limited.status_code == 422
    assert range_error.status_code == 400
