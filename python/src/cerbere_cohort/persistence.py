"""
Persistance SQLite pour Cerbere Cohort.

Sauvegarde l'état du workspace (snapshots, événements, leases) sur disque.
Permet la reprise après arrêt et le replay déterministe.
"""
import json
import sqlite3
import threading
import time

from .merge import ConflictError
from pathlib import Path
from typing import Optional, List, Dict, Any

from .events import CerbereCohortEvent
from .workspace import Workspace, Snapshot, Branch, ContentStore
from .lease import LeaseManager, Lease, LeaseError


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different logical request."""


class PersistentWorkspace:
    """
    Workspace persistant basé sur SQLite.
    
    Sauvegarde automatiquement :
    - Les snapshots (états du workspace)
    - Les événements (journal immuable)
    - Les leases actifs (verrous)
    """
    
    def __init__(self, db_path: str | Path, auto_save: bool = True, require_leases: bool = False):
        """
        Args:
            db_path: Chemin vers le fichier SQLite
            auto_save: Si True, sauvegarde automatiquement à chaque opération
        """
        self.db_path = Path(db_path)
        self.auto_save = auto_save
        self.require_leases = require_leases
        self._lock = threading.RLock()
        
        # Workspace en mémoire : reconstruit par replay du journal au chargement
        self._workspace = Workspace(require_leases=self.require_leases)
        self._persisted_ids: set[str] = set()
        self._persisted_hashes: set[str] = set()
        
        # Connexion SQLite
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        
        # Activer le mode WAL pour de meilleures performances
        self._conn.execute("PRAGMA journal_mode=WAL")
        
        # Initialiser le schéma
        self._init_schema()
        
        # Charger l'état existant si présent
        self._load_from_db()
    
    def _init_schema(self):
        """Crée les tables SQLite si elles n'existent pas."""
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    hash TEXT PRIMARY KEY,
                    content TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    metadata_json TEXT
                );
                
                CREATE TABLE IF NOT EXISTS leases (
                    resource_key TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    acquired_at REAL NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS lease_counters (
                    resource_key TEXT PRIMARY KEY,
                    fencing_token INTEGER NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
                CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
            """)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(leases)")}
            if "fencing_token" not in columns:
                self._conn.execute("ALTER TABLE leases ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
    
    def _load_from_db(self):
        """Reconstruit le workspace par REPLAY du journal (les ids sont vérifiés)."""
        with self._lock:
            self._persisted_ids.clear()
            self._persisted_hashes.clear()
            store = ContentStore()
            for r in self._conn.execute("SELECT hash, content FROM objects"):
                store.load_text(r['hash'], r['content'])
                self._persisted_hashes.add(r['hash'])

            events = []
            for r in self._conn.execute(
                "SELECT event_id, timestamp, agent_id, action, payload_json, metadata_json FROM events ORDER BY rowid"
            ):
                events.append(CerbereCohortEvent(
                    event_id=r['event_id'],
                    timestamp=r['timestamp'],
                    agent_id=r['agent_id'],
                    action=r['action'],
                    payload=json.loads(r['payload_json']),
                    metadata=json.loads(r['metadata_json']) if r['metadata_json'] else None
                ))
                self._persisted_ids.add(r['event_id'])

            self._workspace = Workspace.replay(
                events, store, require_leases=self.require_leases
            )  # lève ReplayError si le journal est incohérent

            for counter in self._conn.execute(
                "SELECT resource_key, fencing_token FROM lease_counters"
            ):
                self._workspace._lease_manager._fencing_counters[counter["resource_key"]] = counter["fencing_token"]
            
            # Charger les leases actifs
            cursor = self._conn.execute(
                "SELECT resource_key, agent_id, expires_at, acquired_at, fencing_token FROM leases"
            )
            for r in cursor:
                lease = Lease(
                    agent_id=r['agent_id'],
                    resource_key=r['resource_key'],
                    expires_at=r['expires_at'],
                    acquired_at=r['acquired_at'],
                    fencing_token=r['fencing_token'],
                )
                self._workspace._lease_manager._active_leases[r['resource_key']] = lease
    
    # === API publique (délègue au workspace + persiste) ===
    
    @property
    def head(self) -> Snapshot:
        return self._workspace.head
    
    def create_branch(self, agent_id: str, branch_name: str) -> Branch:
        """Crée une branche et la persiste."""
        return self._mutate(lambda: self._workspace.create_branch(agent_id, branch_name))
    
    def commit(self, agent_id: str, branch: Branch, resolver=None, max_retries: int = 3,
               expected_head_version: str | None = None) -> Snapshot:
        """Committe et persiste le nouveau snapshot, avec CAS strict optionnel."""
        return self._mutate(
            lambda: self._workspace.commit(
                agent_id, branch, resolver=resolver, max_retries=max_retries,
                expected_head_version=expected_head_version,
            )
        )

    def execute_idempotent(self, scope: str, idempotency_key: str,
                           request_hash: str, operation):
        """Run an operation once and persist its response in the same DB transaction.

        The callback must perform its state changes using this workspace's
        in-memory engine and the shared connection. Its changes, event journal,
        API metadata, and idempotency response are committed atomically.
        """
        if not scope or not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("scope and an idempotency key of at most 200 characters are required")
        if not request_hash:
            raise ValueError("request_hash is required")

        def run_once():
            row = self._conn.execute(
                "SELECT request_hash, response_json FROM idempotency_records "
                "WHERE scope = ? AND idempotency_key = ?",
                (scope, idempotency_key),
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used with a different request"
                    )
                return json.loads(row["response_json"])

            response = operation()
            response_json = json.dumps(
                response, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            self._conn.execute(
                "INSERT INTO idempotency_records "
                "(scope, idempotency_key, request_hash, response_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, idempotency_key, request_hash, response_json, time.time()),
            )
            return response

        return self._mutate(run_once)

    def seed(self, data: Dict[str, Any], agent_id: str = "system") -> Snapshot:
        """Initialise l'état par un commit journalisé et persisté."""
        return self._mutate(lambda: self._workspace.seed(data, agent_id))

    @property
    def store(self) -> ContentStore:
        return self._workspace.store
    
    def acquire_lease(self, agent_id: str, resource_key: str, duration_seconds: float = 30.0, **kwargs) -> Lease:
        """Acquiert un lease sans conserver de transaction pendant une attente.

        En mode persistant, chaque tentative non bloquante utilise sa propre
        transaction SQLite. L'attente et le polling se déroulent hors transaction
        afin qu'un autre processus puisse libérer le lease.
        """
        wait = kwargs.pop("wait", False)
        timeout_seconds = kwargs.pop("timeout_seconds", 5.0)

        if not wait or not self.auto_save:
            def operation():
                lease = self._workspace.acquire_lease(
                    agent_id, resource_key, duration_seconds, wait=wait,
                    timeout_seconds=timeout_seconds, **kwargs
                )
                self._save_lease(lease)
                return lease
            return self._mutate(operation)

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                def try_acquire():
                    lease = self._workspace.acquire_lease(
                        agent_id, resource_key, duration_seconds, wait=False, **kwargs
                    )
                    self._save_lease(lease)
                    return lease

                return self._mutate(try_acquire)
            except LeaseError as exc:
                if exc.code != "lease_held":
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holder = (exc.details or {}).get("holder", "unknown")
                    raise LeaseError(
                        f"Timeout après {timeout_seconds}s en attendant "
                        f"'{resource_key}' (détenu par '{holder}')",
                        code="lease_wait_timeout",
                        details={
                            "resource_key": resource_key,
                            "holder": holder,
                            "timeout_seconds": timeout_seconds,
                        },
                    ) from exc
                time.sleep(min(0.02, remaining))
    
    def release_lease(self, agent_id: str, resource_key: str) -> None:
        """Libère un lease et le persiste."""
        def operation():
            self._workspace.release_lease(agent_id, resource_key)
            self._delete_lease(resource_key)
        self._mutate(operation)
    
    def refresh(self) -> None:
        """Reload persisted state while holding the cross-process database lock."""
        if not self.auto_save:
            self._load_from_db()
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._load_from_db()
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def get_events(self) -> List[CerbereCohortEvent]:
        return self._workspace.get_events()
    
    def _mutate(self, operation):
        """
        Exécute une mutation comme une seule transaction SQLite inter-processus.

        BEGIN IMMEDIATE sérialise les writers. Le replay est refait APRÈS
        l'acquisition du verrou SQLite : une instance ancienne ne peut donc
        jamais committer contre un HEAD périmé.
        """
        if not self.auto_save:
            return operation()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._load_from_db()
                try:
                    result = operation()
                except ConflictError:
                    # Un conflit est un résultat métier journalisé, pas une
                    # erreur de persistance : conserver ses événements.
                    self._save_new_objects()
                    self._save_new_events()
                    self._conn.commit()
                    raise
                self._save_new_objects()
                self._save_new_events()
                self._conn.commit()
                return result
            except ConflictError:
                raise
            except BaseException:
                self._conn.rollback()
                # The in-memory workspace may already have been mutated before
                # a later guard (for example task fencing) rejects publication.
                # Rebuild it from the rolled-back journal so this process cannot
                # observe state that was never committed to durable storage.
                try:
                    self._load_from_db()
                except Exception:
                    pass
                raise

    # === Méthodes de persistance ===
    
    def _save_new_objects(self):
        """Persiste les contenus pas encore écrits. Toujours AVANT les événements qui les référencent."""
        objects = self._workspace.store.dump()
        with self._lock:
            for digest, text in objects.items():
                if digest not in self._persisted_hashes:
                    self._conn.execute("INSERT OR IGNORE INTO objects (hash, content) VALUES (?, ?)", (digest, text))
                    self._persisted_hashes.add(digest)
    
    def _save_lease(self, lease: Lease):
        """Sauvegarde un lease dans SQLite."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO leases (resource_key, agent_id, expires_at, acquired_at, fencing_token) VALUES (?, ?, ?, ?, ?)",
                (lease.resource_key, lease.agent_id, lease.expires_at, lease.acquired_at, lease.fencing_token)
            )
            self._conn.execute(
                """INSERT INTO lease_counters (resource_key, fencing_token) VALUES (?, ?)
                   ON CONFLICT(resource_key) DO UPDATE SET fencing_token = MAX(fencing_token, excluded.fencing_token)""",
                (lease.resource_key, lease.fencing_token),
            )
    
    def _delete_lease(self, resource_key: str):
        """Supprime un lease de SQLite."""
        with self._lock:
            self._conn.execute("DELETE FROM leases WHERE resource_key = ?", (resource_key,))
    
    def _save_new_events(self):
        """Persiste tous les événements pas encore écrits (workspace ET leases)."""
        events = self._workspace.get_events()
        with self._lock:
            for event in events:
                if event.event_id in self._persisted_ids:
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO events (event_id, timestamp, agent_id, action, payload_json, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.timestamp,
                        event.agent_id,
                        event.action,
                        json.dumps(event.payload, ensure_ascii=False),
                        json.dumps(event.metadata, ensure_ascii=False) if event.metadata else None,
                    ),
                )
                self._persisted_ids.add(event.event_id)

    # === Méthodes utilitaires ===
    
    def get_snapshot_count(self) -> int:
        """Retourne le nombre total de snapshots."""
        return len(self._workspace._snapshots)
    
    def get_event_count(self) -> int:
        """Retourne le nombre total d'événements."""
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM events")
            return cursor.fetchone()[0]
    
    def get_active_lease_count(self) -> int:
        """Retourne le nombre de leases actifs."""
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM leases")
            return cursor.fetchone()[0]
    
    def export_events_jsonl(self, output_path: str | Path) -> int:
        """
        Exporte tous les événements au format JSONL.
        Compatible avec le moteur de trace Rust.
        
        Returns:
            Nombre d'événements exportés
        """
        output_path = Path(output_path)
        count = 0
        
        with self._lock:
            cursor = self._conn.execute(
                "SELECT event_id, timestamp, agent_id, action, payload_json, metadata_json FROM events ORDER BY rowid"
            )
            
            with open(output_path, 'w', encoding='utf-8') as f:
                for row in cursor:
                    event = CerbereCohortEvent(
                        event_id=row['event_id'],
                        timestamp=row['timestamp'],
                        agent_id=row['agent_id'],
                        action=row['action'],
                        payload=json.loads(row['payload_json']),
                        metadata=json.loads(row['metadata_json']) if row['metadata_json'] else None
                    )
                    f.write(event.to_jsonl() + '\n')
                    count += 1
        
        return count
    
    def close(self):
        """Ferme la connexion SQLite."""
        with self._lock:
            self._conn.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
