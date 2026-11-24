"""Durable task ownership, leases, fencing, retries, and lifecycle transitions."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any


class TaskError(ValueError):
    def __init__(self, message: str, *, code: str = "task_error", status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def ensure_task_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'assigned', 'running', 'completed', 'failed')),
            assigned_agent_id TEXT,
            lease_expires_at DOUBLE PRECISION,
            fencing_token BIGINT NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            result_hash TEXT,
            completion_key TEXT,
            error TEXT,
            creation_key TEXT UNIQUE,
            creation_hash TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_task_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            agent_id TEXT,
            action TEXT NOT NULL,
            fencing_token BIGINT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )"""
    )
    conn.commit()


class TaskCoordinator:
    """Database-backed task state machine; safe across independent DB clients."""

    def __init__(self, connection):
        self._conn = connection
        ensure_task_schema(connection)

    def _begin(self):
        self._conn.execute("BEGIN IMMEDIATE")

    def _event(self, task_id, agent_id, action, token, payload=None):
        self._conn.execute(
            """INSERT INTO agent_task_events
               (event_id, task_id, agent_id, action, fencing_token, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, agent_id, action, token, _json(payload or {}), time.time()),
        )

    def _row(self, task_id):
        row = self._conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("Task not found", code="task_not_found", status_code=404)
        return row

    @staticmethod
    def _serialize(row):
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
            "assigned_agent_id": row["assigned_agent_id"],
            "lease_expires_at": row["lease_expires_at"],
            "fencing_token": row["fencing_token"],
            "attempt_count": row["attempt_count"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create(self, title: str, payload: Any, idempotency_key: str | None = None):
        if not title or not title.strip():
            raise TaskError("Task title is required", code="invalid_task", status_code=422)
        payload_json = _json(payload)
        request_hash = _hash({"title": title.strip(), "payload": payload})
        task_id = str(uuid.uuid4())
        now = time.time()
        self._begin()
        try:
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT * FROM agent_tasks WHERE creation_key = ?", (idempotency_key,)
                ).fetchone()
                if existing:
                    if existing["creation_hash"] != request_hash:
                        raise TaskError(
                            "Idempotency key was reused with a different task",
                            code="idempotency_key_reused",
                        )
                    self._conn.commit()
                    return self._serialize(existing)
            self._conn.execute(
                """INSERT INTO agent_tasks
                   (task_id, title, payload_json, status, fencing_token, attempt_count,
                    creation_key, creation_hash, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', 0, 0, ?, ?, ?, ?)""",
                (task_id, title.strip(), payload_json, idempotency_key, request_hash, now, now),
            )
            self._event(task_id, None, "task.created", 0, {"title": title.strip()})
            row = self._row(task_id)
            self._conn.commit()
            return self._serialize(row)
        except BaseException:
            self._conn.rollback()
            raise

    def get(self, task_id: str):
        return self._serialize(self._row(task_id))

    def list(self, limit: int = 100):
        if limit < 1 or limit > 500:
            raise TaskError("limit must be between 1 and 500", code="invalid_limit", status_code=422)
        rows = self._conn.execute(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._serialize(row) for row in rows]

    def claim(self, task_id: str, agent_id: str, lease_seconds: float = 60):
        if not agent_id or not agent_id.strip() or not 1 <= lease_seconds <= 86400:
            raise TaskError("Invalid agent or lease duration", code="invalid_claim", status_code=422)
        now = time.time()
        self._begin()
        try:
            row = self._row(task_id)
            active = row["lease_expires_at"] is not None and row["lease_expires_at"] > now
            if row["status"] in {"completed", "failed"}:
                raise TaskError("Task is terminal", code="task_terminal")
            if active:
                if row["assigned_agent_id"] == agent_id:
                    self._conn.execute(
                        "UPDATE agent_tasks SET lease_expires_at = ?, updated_at = ? WHERE task_id = ?",
                        (now + lease_seconds, now, task_id),
                    )
                    result = self._row(task_id)
                    self._conn.commit()
                    return self._serialize(result)
                raise TaskError("Task is already leased", code="task_leased")
            token = int(row["fencing_token"]) + 1
            attempt = int(row["attempt_count"]) + 1
            self._conn.execute(
                """UPDATE agent_tasks SET status = 'assigned', assigned_agent_id = ?,
                   lease_expires_at = ?, fencing_token = ?, attempt_count = ?, updated_at = ?
                   WHERE task_id = ?""",
                (agent_id, now + lease_seconds, token, attempt, now, task_id),
            )
            self._event(task_id, agent_id, "task.assigned", token, {"attempt": attempt})
            result = self._row(task_id)
            self._conn.commit()
            return self._serialize(result)
        except BaseException:
            self._conn.rollback()
            raise

    def _owned(self, task_id, agent_id, fencing_token, now):
        row = self._row(task_id)
        if row["assigned_agent_id"] != agent_id or int(row["fencing_token"]) != int(fencing_token):
            raise TaskError("Stale task owner or fencing token", code="task_fenced")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
            raise TaskError("Task lease expired", code="task_lease_expired")
        return row

    def start(self, task_id: str, agent_id: str, fencing_token: int):
        now = time.time()
        self._begin()
        try:
            row = self._owned(task_id, agent_id, fencing_token, now)
            if row["status"] == "running":
                self._conn.commit()
                return self._serialize(row)
            if row["status"] != "assigned":
                raise TaskError("Only an assigned task can start", code="invalid_task_transition")
            self._conn.execute(
                "UPDATE agent_tasks SET status = 'running', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            self._event(task_id, agent_id, "task.started", fencing_token)
            result = self._row(task_id)
            self._conn.commit()
            return self._serialize(result)
        except BaseException:
            self._conn.rollback()
            raise

    def heartbeat(self, task_id: str, agent_id: str, fencing_token: int, lease_seconds: float = 60):
        if not 1 <= lease_seconds <= 86400:
            raise TaskError("Invalid lease duration", code="invalid_lease_duration", status_code=422)
        now = time.time()
        self._begin()
        try:
            row = self._owned(task_id, agent_id, fencing_token, now)
            if row["status"] not in {"assigned", "running"}:
                raise TaskError("Task is not active", code="invalid_task_transition")
            self._conn.execute(
                "UPDATE agent_tasks SET lease_expires_at = ?, updated_at = ? WHERE task_id = ?",
                (now + lease_seconds, now, task_id),
            )
            self._event(task_id, agent_id, "task.heartbeat", fencing_token)
            result = self._row(task_id)
            self._conn.commit()
            return self._serialize(result)
        except BaseException:
            self._conn.rollback()
            raise

    def complete(self, task_id: str, agent_id: str, fencing_token: int,
                 result: Any, idempotency_key: str):
        if not idempotency_key or len(idempotency_key) > 200:
            raise TaskError("A completion idempotency key is required", code="invalid_idempotency_key", status_code=422)
        result_json = _json(result)
        result_hash = _hash(result)
        now = time.time()
        self._begin()
        try:
            row = self._row(task_id)
            if row["status"] == "completed":
                if row["completion_key"] == idempotency_key and row["result_hash"] == result_hash:
                    self._conn.commit()
                    return self._serialize(row)
                raise TaskError("Task was already completed with a different result", code="task_already_completed")
            row = self._owned(task_id, agent_id, fencing_token, now)
            if row["status"] != "running":
                raise TaskError("Only a running task can complete", code="invalid_task_transition")
            self._conn.execute(
                """UPDATE agent_tasks SET status = 'completed', result_json = ?,
                   result_hash = ?, completion_key = ?, lease_expires_at = NULL,
                   updated_at = ? WHERE task_id = ?""",
                (result_json, result_hash, idempotency_key, now, task_id),
            )
            self._event(task_id, agent_id, "task.completed", fencing_token, {"result_hash": result_hash})
            completed = self._row(task_id)
            self._conn.commit()
            return self._serialize(completed)
        except BaseException:
            self._conn.rollback()
            raise

    def fail(self, task_id: str, agent_id: str, fencing_token: int, error: str):
        if not error or len(error) > 4000:
            raise TaskError("A failure reason of at most 4000 characters is required", code="invalid_task_error", status_code=422)
        now = time.time()
        self._begin()
        try:
            row = self._owned(task_id, agent_id, fencing_token, now)
            if row["status"] not in {"assigned", "running"}:
                raise TaskError("Task is not active", code="invalid_task_transition")
            self._conn.execute(
                """UPDATE agent_tasks SET status = 'failed', error = ?,
                   lease_expires_at = NULL, updated_at = ? WHERE task_id = ?""",
                (error, now, task_id),
            )
            self._event(task_id, agent_id, "task.failed", fencing_token, {"error": error})
            result = self._row(task_id)
            self._conn.commit()
            return self._serialize(result)
        except BaseException:
            self._conn.rollback()
            raise

    def events(self, task_id: str):
        self._row(task_id)
        rows = self._conn.execute(
            "SELECT * FROM agent_task_events WHERE task_id = ? ORDER BY created_at, event_id",
            (task_id,),
        ).fetchall()
        return [{
            "event_id": row["event_id"], "task_id": row["task_id"],
            "agent_id": row["agent_id"], "action": row["action"],
            "fencing_token": row["fencing_token"], "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        } for row in rows]
