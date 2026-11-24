"""PostgreSQL adapter for Cohort's replay-verified persistence engine."""
from __future__ import annotations

import json
import re
import threading
from uuid import UUID

from .events import CerbereCohortEvent
from .lease import Lease
from .workspace import ContentStore

from .persistence import PersistentWorkspace


class _Row(dict):
    """Dict row that also supports sqlite-style positional indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    # PostgreSQL commands such as CREATE TABLE and INSERT may return no
    # result columns. Psycopg still asks for a row factory while processing
    # the command, so provide a harmless tuple factory in that case.
    if cursor.description is None:
        return lambda values: tuple(values)
    names = [column.name for column in cursor.description]
    def make_row(values):
        return _Row(zip(names, values))
    return make_row


class _Connection:
    """Translate the small SQLite SQL subset used by PersistentWorkspace.

    Supabase/Render drop idle connections. A single long-lived connection would
    leave the API returning 500s until restart, so statements issued outside a
    transaction transparently reconnect (once) and retry.
    """
    def __init__(self, dsn: str, schema: str = "public"):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary]>=3.2 for PostgreSQL support") from exc
        self._psycopg = psycopg
        self._validate_schema(schema)
        self._dsn = dsn
        self.schema = schema
        self._lock = threading.RLock()
        self._in_txn = False
        self._transaction_lock_held = False
        self._db = self._connect()
        if self.schema != "public":
            self._db.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')

    @staticmethod
    def _validate_schema(schema: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("Invalid PostgreSQL schema name")

    def _connect(self):
        # prepare_threshold=None keeps the connection compatible with PgBouncer
        # (Supabase pooler); TCP keepalives detect dead peers early.
        return self._psycopg.connect(
            self._dsn, row_factory=_row_factory, connect_timeout=15, autocommit=True,
            prepare_threshold=None, keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=3,
        )

    def _reconnect(self):
        try:
            self._db.close()
        except Exception:
            pass
        self._db = self._connect()

    @staticmethod
    def _translate(sql: str, schema: str = "public") -> str:
        statement = sql.strip()
        upper = statement.upper()
        if upper == "BEGIN IMMEDIATE":
            return "BEGIN"
        ignore_insert = bool(re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO", statement, flags=re.I))
        statement = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement, flags=re.I)
        if re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO", statement, flags=re.I):
            statement = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", statement, flags=re.I)
            match = re.search(r"INSERT INTO\s+([\w]+)\s*\(([^)]+)\)\s*VALUES", statement, flags=re.I)
            if not match:
                raise ValueError("Unsupported INSERT OR REPLACE statement")
            table = match.group(1)
            columns = [c.strip() for c in match.group(2).split(",")]
            key = "resource_key" if table.lower() == "leases" else columns[0]
            updates = [c for c in columns if c != key]
            clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in updates)
            statement += f" ON CONFLICT ({key}) DO UPDATE SET {clause}"
        # Keep Cohort's tables isolated from generic tables that may already
        # exist in a shared Supabase/Postgres database (notably public.events).
        # Apply this mapping to reads, writes, indexes, and branch storage.
        # Preserve the adapter's established public-schema SQL contract.
        # Tenant schemas are always explicitly qualified to prevent search_path
        # from ever crossing organization boundaries.
        public = schema == "public"
        namespaced = {
            "objects": "cohort_objects" if public else f'{schema}.cohort_objects',
            "events": f'{schema}.cohort_events',
            "leases": f'{schema}.cohort_leases',
            "metadata": f'{schema}.cohort_metadata',
            "lease_counters": f'{schema}.cohort_lease_counters',
            "idempotency_records": f'{schema}.cohort_idempotency_records',
            "agent_tasks": f'{schema}.cohort_agent_tasks',
            "agent_task_events": f'{schema}.cohort_agent_task_events',
            "api_branches": "cohort_api_branches" if public else f'{schema}.cohort_api_branches',
            "mcp_branches": "cohort_mcp_branches" if public else f'{schema}.cohort_mcp_branches',
            "idx_events_timestamp": 'cohort_idx_events_timestamp',
            "idx_events_agent": 'cohort_idx_events_agent',
            "idx_events_action": 'cohort_idx_events_action',
        }
        for source, target in namespaced.items():
            if source == "metadata":
                # "metadata" is also a real column on cohort_audit_logs.
                # Rewrite only the legacy persistence TABLE reference, never
                # identically named columns in unrelated Cohort tables.
                statement = re.sub(
                    r"((?:\b(?:FROM|JOIN|INTO|UPDATE)\s+)|(?:\bTABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?))metadata\b",
                    lambda match: match.group(1) + target,
                    statement,
                    flags=re.I,
                )
            else:
                statement = re.sub(rf"\b{source}\b", target, statement, flags=re.I)
        statement = re.sub(r"ORDER\s+BY\s+rowid", "ORDER BY seq", statement, flags=re.I)
        counter_table = "cohort_lease_counters" if schema == "public" else f"{schema}.cohort_lease_counters"
        statement = statement.replace(
            "MAX(fencing_token, excluded.fencing_token)",
            f"GREATEST({counter_table}.fencing_token, EXCLUDED.fencing_token)",
        )
        if ignore_insert and "ON CONFLICT" not in statement.upper():
            statement += " ON CONFLICT DO NOTHING"
        return statement.replace("?", "%s")

    def _run(self, sql: str, params):
        # Lightweight adapter instances may omit schema; retain public default.
        schema = getattr(self, "schema", "public")
        cursor = self._db.execute(self._translate(sql, schema), params)
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._in_txn = True
            self._db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"cerbere-cohort:{schema}",),
            )
        return cursor

    def execute(self, sql: str, params=()):
        begins_transaction = sql.strip().upper() == "BEGIN IMMEDIATE"
        if begins_transaction:
            # Keep the shared connection exclusively owned until commit/rollback.
            self._lock.acquire()
            if self._in_txn:
                self._lock.release()
                raise RuntimeError("A PostgreSQL transaction is already active on this connection")
            try:
                if self._db.closed or getattr(self._db, "broken", False):
                    self._reconnect()
                try:
                    cursor = self._run(sql, params)
                except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                    if self._in_txn:
                        raise
                    self._reconnect()
                    cursor = self._run(sql, params)
                self._transaction_lock_held = True
                return cursor
            except BaseException:
                if self._in_txn:
                    try:
                        self._db.rollback()
                    except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                        self._reconnect()
                    self._in_txn = False
                self._lock.release()
                raise

        with self._lock:
            if not self._in_txn and (self._db.closed or getattr(self._db, "broken", False)):
                self._reconnect()
            try:
                return self._run(sql, params)
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                if self._in_txn:
                    # Replaying a partially executed transaction is unsafe.
                    raise
                self._reconnect()
                return self._run(sql, params)

    def commit(self):
        release_transaction_lock = False
        try:
            with self._lock:
                try:
                    self._db.commit()
                except BaseException:
                    try:
                        self._db.rollback()
                    except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                        self._reconnect()
                    raise
                finally:
                    self._in_txn = False
                    if getattr(self, "_transaction_lock_held", False):
                        self._transaction_lock_held = False
                        release_transaction_lock = True
        finally:
            if release_transaction_lock:
                self._lock.release()

    def rollback(self):
        release_transaction_lock = False
        try:
            with self._lock:
                try:
                    self._db.rollback()
                except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                    self._reconnect()
                finally:
                    self._in_txn = False
                    if getattr(self, "_transaction_lock_held", False):
                        self._transaction_lock_held = False
                        release_transaction_lock = True
        finally:
            if release_transaction_lock:
                self._lock.release()

    def close(self):
        with self._lock:
            if self._in_txn:
                self.rollback()
            self._db.close()


class PostgresPersistentWorkspace(PersistentWorkspace):
    """PersistentWorkspace backed by Supabase PostgreSQL.

    DATABASE_URL is read from the deployment environment by the caller.
    The advisory transaction lock serializes workspace writes across instances.
    """
    def __init__(self, database_url: str, auto_save: bool = True,
                 require_leases: bool = False, organization_id: str | None = None):
        if not database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError("A PostgreSQL DATABASE_URL is required")
        self.db_path = database_url
        self.organization_id = str(UUID(organization_id)) if organization_id else None
        # Each organization gets a dedicated PostgreSQL schema and connection.
        # The default public schema preserves compatibility for local adapter tests.
        self.schema = f'cohort_org_{UUID(self.organization_id).hex}' if self.organization_id else 'public'
        self.auto_save = auto_save
        self.require_leases = require_leases
        self._lock = threading.RLock()
        from .workspace import Workspace
        self._workspace = Workspace(require_leases=require_leases)
        self._persisted_ids: set[str] = set()
        self._persisted_hashes: set[str] = set()
        self._conn = _Connection(database_url, schema=self.schema)
        self._init_schema()
        self._load_from_db()

    def _init_schema(self):
        schema = (
            """CREATE TABLE IF NOT EXISTS objects (
                hash TEXT PRIMARY KEY, content TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS events (
                seq BIGSERIAL UNIQUE, event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                agent_id TEXT NOT NULL, action TEXT NOT NULL,
                payload_json TEXT NOT NULL, metadata_json TEXT)""",
            """CREATE TABLE IF NOT EXISTS leases (
                resource_key TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL, acquired_at DOUBLE PRECISION NOT NULL,
                fencing_token BIGINT NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS idempotency_records (
                scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (scope, idempotency_key))""",
            """CREATE TABLE IF NOT EXISTS lease_counters (
                resource_key TEXT PRIMARY KEY, fencing_token BIGINT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_action ON events(action)",
        )
        with self._lock:
            for statement in schema:
                self._conn.execute(statement)
            # Upgrade databases created before fencing tokens were introduced.
            # IF NOT EXISTS keeps this safe to run on every application startup.
            self._conn.execute(
                "ALTER TABLE leases ADD COLUMN IF NOT EXISTS "
                "fencing_token BIGINT NOT NULL DEFAULT 0"
            )
            self._conn.commit()


    def _load_from_db(self):
        """Reload persisted state using PostgreSQL SQL (never SQLite PRAGMA/rowid)."""
        with self._lock:
            self._persisted_ids.clear()
            self._persisted_hashes.clear()
            store = ContentStore()

            for row in self._conn.execute(
                "SELECT hash, content FROM objects"
            ):
                store.load_text(row["hash"], row["content"])
                self._persisted_hashes.add(row["hash"])

            events = []
            for row in self._conn.execute(
                "SELECT event_id, timestamp, agent_id, action, payload_json, metadata_json "
                "FROM events ORDER BY seq"
            ):
                events.append(CerbereCohortEvent(
                    event_id=row["event_id"],
                    timestamp=row["timestamp"],
                    agent_id=row["agent_id"],
                    action=row["action"],
                    payload=json.loads(row["payload_json"]),
                    metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
                ))
                self._persisted_ids.add(row["event_id"])

            self._workspace = self._workspace.replay(
                events, store, require_leases=self.require_leases
            )

            for row in self._conn.execute(
                "SELECT resource_key, fencing_token FROM lease_counters"
            ):
                self._workspace._lease_manager._fencing_counters[
                    row["resource_key"]
                ] = row["fencing_token"]

            for row in self._conn.execute(
                "SELECT resource_key, agent_id, expires_at, acquired_at, fencing_token FROM leases"
            ):
                lease = Lease(
                    agent_id=row["agent_id"],
                    resource_key=row["resource_key"],
                    expires_at=row["expires_at"],
                    acquired_at=row["acquired_at"],
                    fencing_token=row["fencing_token"],
                )
                self._workspace._lease_manager._active_leases[
                    row["resource_key"]
                ] = lease
