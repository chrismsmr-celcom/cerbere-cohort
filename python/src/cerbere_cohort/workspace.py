# python/src/cerbere_cohort/workspace.py
import base64
import copy
import json
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .events import CerbereCohortEvent
from .errors import CerbereError
from .hashing import ContentError, canonical_json, content_hash, hash_text, snapshot_id
from .lease import LeaseError, LeaseManager
from .merge import ConflictError, detect_and_merge
from .resolve import Conflict, Resolution, ResolutionError, Resolver, Side


_MISSING = object()


class ReplayError(CerbereError):
    """Le journal ne permet pas de reconstruire fidèlement l'état."""
    code = "replay_error"


class ContentStore:
    """Magasin adressé par le contenu : hash -> JSON canonique (append-only)."""

    def __init__(self) -> None:
        self._objects: Dict[str, str] = {}

    def put(self, value: Any) -> str:
        text = canonical_json(value)
        digest = hash_text(text)
        self._objects.setdefault(digest, text)
        return digest

    def get(self, digest: str) -> Any:
        """Retourne une copie fraîche de la valeur (jamais l'objet interne)."""
        return json.loads(self._objects[digest])

    def has(self, digest: str) -> bool:
        return digest in self._objects

    def load_text(self, digest: str, text: str) -> None:
        """Charge un objet persisté en vérifiant que son hash correspond."""
        if hash_text(text) != digest:
            raise ReplayError(
                f"objet corrompu : le contenu ne correspond pas au hash {digest[:12]}",
                details={"digest": digest},
            )
        self._objects.setdefault(digest, text)

    def dump(self) -> Dict[str, str]:
        return dict(self._objects)


@dataclass(frozen=True)
class Snapshot:
    """Version immuable : mapping clé -> hash de contenu, plus les ids des parents."""
    version_id: str
    entries: Mapping[str, str]
    parents: Tuple[str, ...] = ()
    store: ContentStore = field(default=None, repr=False, compare=False)  # type: ignore[assignment]

    @property
    def parent_version(self) -> Optional[str]:
        return self.parents[0] if self.parents else None

    @property
    def data(self) -> Dict[str, Any]:
        """Copie fraîche des valeurs : la modifier ne change pas le snapshot."""
        return {key: self.store.get(digest) for key, digest in self.entries.items()}


def _make_snapshot(entries: Mapping[str, str], parents: Tuple[str, ...], store: ContentStore) -> Snapshot:
    frozen = MappingProxyType(dict(sorted(entries.items())))
    return Snapshot(version_id=snapshot_id(frozen, parents), entries=frozen, parents=parents, store=store)


class Branch:
    """Une copie de travail isolée pour un agent."""

    def __init__(self, name: str, base_snapshot: Snapshot, lease_manager: LeaseManager,
                 agent_id: str, require_leases: bool = False):
        self.name = name
        self.current_data: Dict[str, Any] = base_snapshot.data
        self.base_version = base_snapshot.version_id
        self._lease_manager = lease_manager
        self._agent_id = agent_id
        self._require_leases = require_leases
        self.intents: Dict[str, str] = {}  # clé -> pourquoi (provenance, jamais utilisé pour décider)
        self.fencing_tokens: Dict[str, int] = {}
        self._lease_exempt = False

    def _check_write_allowed(self, key: str, verb: str) -> None:
        if self._require_leases and not self._lease_manager.check_lease(self._agent_id, key):
            raise LeaseError(
                f"L'agent '{self._agent_id}' doit acquérir un lease sur '{key}' "
                f"avant de pouvoir le {verb}.",
                code="lease_required",
                details={
                    "agent_id": self._agent_id,
                    "key": key,
                    "operation": verb,
                },
            )
        if self._require_leases:
            lease = self._lease_manager.get_active_leases().get(key)
            if lease is None or lease.agent_id != self._agent_id:
                raise LeaseError("Lease absent ou expiré.", code="lease_required")
            self.fencing_tokens[key] = lease.fencing_token

    def _put(self, key: str, value: Any) -> None:
        if not isinstance(key, str):
            raise ContentError(f"les clés doivent être des str (reçu {type(key).__name__})")
        # Valide ET normalise (copie profonde, types JSON) : ce qu'on lit == ce qu'on commit.
        self.current_data[key] = json.loads(canonical_json(value))

    def set(self, key: str, value: Any, intent: Optional[str] = None) -> None:
        self._check_write_allowed(key, "modifier")
        self._put(key, value)
        self._note_intent(key, intent)

    def _note_intent(self, key: str, intent: Optional[str]) -> None:
        if intent:
            self.intents[key] = intent
        else:
            self.intents.pop(key, None)

    def get(self, key: str, default: Any = None) -> Any:
        return self.current_data.get(key, default)

    def delete(self, key: str, intent: Optional[str] = None) -> None:
        self._check_write_allowed(key, "supprimer")
        self.current_data.pop(key, None)
        self._note_intent(key, intent)


class Workspace:
    """Le gestionnaire central du workspace partagé."""

    def __init__(self, require_leases: bool = False, store: Optional[ContentStore] = None):
        self._require_leases = require_leases
        self._store = store if store is not None else ContentStore()
        self._lock = threading.RLock()  # un seul écrivain à la fois sur HEAD (CONCEPTS §8)
        genesis = _make_snapshot({}, (), self._store)  # id déterministe
        self._snapshots: Dict[str, Snapshot] = {genesis.version_id: genesis}
        self._head_version = genesis.version_id
        self._events: List[CerbereCohortEvent] = []
        self._lease_manager = LeaseManager()

    @property
    def head(self) -> Snapshot:
        return self._snapshots[self._head_version]

    @property
    def store(self) -> ContentStore:
        return self._store

    def create_branch(self, agent_id: str, branch_name: str) -> Branch:
        with self._lock:
            branch = Branch(
                name=branch_name,
                base_snapshot=self.head,
                lease_manager=self._lease_manager,
                agent_id=agent_id,
                require_leases=self._require_leases,
            )
            self._events.append(CerbereCohortEvent(
                agent_id=agent_id, action="workspace.branch",
                payload={"branch_name": branch_name, "base_version": self._head_version},
            ))
        return branch

    def commit(self, agent_id: str, branch: Branch, resolver: Optional[Resolver] = None,
               max_retries: int = 3,
               expected_head_version: Optional[str] = None) -> Snapshot:
        """Valide une branche : fusion à trois voies, jamais d'écrasement silencieux.

        Sans `resolver`, un conflit annule le commit (ConflictError). Avec un résolveur,
        chaque conflit reçoit une décision enregistrée (`coordination.resolved`). Le résolveur
        est appelé HORS du verrou (un humain ou un modèle peut être lent) ; si HEAD a bougé
        pendant ce temps, la fusion est refaite contre le nouveau HEAD (CONCEPTS §4).

        If expected_head_version is supplied, this is a strict compare-and-swap:
        it must equal the branch's read version and the current HEAD at commit time.
        A mismatch is journaled and rejected without attempting a three-way merge.
        """
        if expected_head_version is not None and expected_head_version != branch.base_version:
            with self._lock:
                actual_head_version = self._head_version
                self._events.append(CerbereCohortEvent(
                    agent_id=agent_id, action="coordination.cas_rejected",
                    payload={
                        "branch_name": branch.name,
                        "expected_head_version": expected_head_version,
                        "branch_base_version": branch.base_version,
                        "actual_head_version": actual_head_version,
                        "reason": "expected_version_does_not_match_branch_base",
                    },
                ))
            raise ConflictError(
                "CAS refusé : la version attendue ne correspond pas à la version lue par cette branche.",
                conflicting_keys=[],
                code="cas_base_mismatch",
                expected_head_version=expected_head_version,
                branch_base_version=branch.base_version,
                actual_head_version=actual_head_version,
            )

        for _ in range(max_retries + 1):
            with self._lock, self._lease_manager.fencing_guard():
                head = self.head
                if expected_head_version is not None and head.version_id != expected_head_version:
                    self._events.append(CerbereCohortEvent(
                        agent_id=agent_id, action="coordination.cas_rejected",
                        payload={
                            "branch_name": branch.name,
                            "expected_head_version": expected_head_version,
                            "branch_base_version": branch.base_version,
                            "actual_head_version": head.version_id,
                            "reason": "head_version_changed",
                        },
                    ))
                    raise ConflictError(
                        "CAS refusé : HEAD a changé depuis la lecture. Rechargez l'état et réessayez.",
                        conflicting_keys=[],
                        code="cas_mismatch",
                        expected_head_version=expected_head_version,
                        actual_head_version=head.version_id,
                        branch_name=branch.name,
                    )
                base = self._snapshots[branch.base_version]
                self._validate_branch_leases(agent_id, branch, base)
                base_data = base.data
                head_data = head.data
                branch_data = copy.deepcopy(branch.current_data)
                branch_entries = {key: content_hash(value) for key, value in branch_data.items()}
                result = detect_and_merge(base_data, branch_data, head_data)
                if result.success:
                    merged_entries = {
                        key: self._store.put(value)
                        for key, value in (result.merged_data or {}).items()
                    }
                    return self._finalize(
                        agent_id, branch, base, head, branch_entries, merged_entries, {}, None
                    )

                conflicts = [
                    self._build_conflict(
                        key, agent_id, branch, base_data, branch_data, head_data
                    )
                    for key in result.conflicting_keys
                ]
                self._events.append(CerbereCohortEvent(
                    agent_id=agent_id, action="coordination.conflict",
                    payload={"branch_name": branch.name, "base_version": branch.base_version,
                             "head_version": head.version_id,
                             "conflicting_keys": result.conflicting_keys},
                ))
                if resolver is None:
                    self._abort(agent_id, branch, head, result.conflicting_keys, result.conflicting_keys,
                                None, "no_resolver")
                    raise ConflictError(
                        f"Conflit détecté lors du commit de l'agent {agent_id}. "
                        f"Les clés suivantes ont été modifiées simultanément : {result.conflicting_keys}",
                        conflicting_keys=result.conflicting_keys,
                        branch_name=branch.name,
                        base_version=branch.base_version,
                        head_version=head.version_id,
                    )

            decisions = self._decide(agent_id, branch, head, resolver, conflicts)  # hors verrou

            with self._lock, self._lease_manager.fencing_guard():
                if self._head_version != head.version_id:
                    continue  # HEAD a bougé pendant la décision : on refait la fusion
                # The resolver ran outside the lock: the lease may have expired or
                # been taken over while it was deciding. Revalidate before committing.
                self._validate_branch_leases(agent_id, branch, base)
                resolved_data = copy.deepcopy(result.merged_data or {})
                for path, decision in decisions.items():
                    self._apply_resolution_path(resolved_data, path, decision)
                merged_entries = {
                    key: self._store.put(value) for key, value in resolved_data.items()
                }
                return self._finalize(
                    agent_id, branch, base, head, branch_entries,
                    merged_entries, decisions, resolver.name
                )

        with self._lock:
            self._abort(agent_id, branch, self.head, [], [], getattr(resolver, "name", None), "head_kept_moving")
        raise ConflictError(
            f"HEAD a changé {max_retries + 1} fois pendant la résolution : commit annulé, rien n'a été écrasé.",
            conflicting_keys=[],
            code="conflict_head_moved",
            attempts=max_retries + 1,
        )

    def _validate_branch_leases(self, agent_id: str, branch: Branch, base: Snapshot) -> None:
        """Reject commits whose write leases expired or were replaced."""
        if not self._require_leases or getattr(branch, "_lease_exempt", False):
            return
        base_data = base.data
        missing = object()
        changed_keys = set()
        for key in set(base_data) | set(branch.current_data):
            before = base_data.get(key, missing)
            after = branch.current_data.get(key, missing)
            if before is missing or after is missing:
                changed = before is not after
            else:
                # Python considers True == 1; lease enforcement must use the
                # same JSON type identity as content hashing and merge logic.
                changed = canonical_json(before) != canonical_json(after)
            if changed:
                changed_keys.add(key)
        active = self._lease_manager.get_active_leases()
        for key in changed_keys:
            lease = active.get(key)
            expected = branch.fencing_tokens.get(key)
            if (lease is None or lease.agent_id != agent_id or expected is None
                    or lease.fencing_token != expected):
                raise LeaseError(
                    f"Lease obsolète pour '{key}' : le commit de '{agent_id}' est refusé.",
                    code="lease_fenced",
                    details={"resource_key": key, "agent_id": agent_id,
                             "expected_fencing_token": expected,
                             "current_fencing_token": lease.fencing_token if lease else None},
                )

    @staticmethod
    def _conflict_path_tokens(path: str) -> Tuple[str, ...]:
        if path.startswith("root-key:v1:"):
            encoded = path[len("root-key:v1:"): ]
            encoded += "=" * (-len(encoded) % 4)
            try:
                return (base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"),)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ResolutionError(
                    f"identifiant de clé racine invalide : {path}", details={"path": path}
                ) from exc
        if not path.startswith("json-pointer:/"):
            return (path,)
        raw = path[len("json-pointer:/"):]
        return tuple(
            part.replace("~1", "/").replace("~0", "~")
            for part in raw.split("/")
        )

    @staticmethod
    def _read_conflict_path(data: Mapping[str, Any], tokens: Tuple[str, ...]) -> Any:
        value: Any = data
        for token in tokens:
            if not isinstance(value, dict) or token not in value:
                return _MISSING
            value = value[token]
        return value

    @staticmethod
    def _apply_resolution_path(data: Dict[str, Any], path: str, decision: Resolution) -> None:
        tokens = Workspace._conflict_path_tokens(path)
        if len(tokens) == 1:
            if decision.present:
                data[tokens[0]] = copy.deepcopy(decision.value)
            else:
                data.pop(tokens[0], None)
            return

        parent: Any = data
        for token in tokens[:-1]:
            if not isinstance(parent, dict) or token not in parent:
                raise ResolutionError(
                    f"chemin de résolution invalide : {path}",
                    details={"path": path},
                )
            parent = parent[token]
        if not isinstance(parent, dict):
            raise ResolutionError(
                f"le parent du chemin de résolution n'est pas un objet JSON : {path}",
                details={"path": path},
            )
        if decision.present:
            parent[tokens[-1]] = copy.deepcopy(decision.value)
        else:
            parent.pop(tokens[-1], None)

    def _build_conflict(
        self, key: str, agent_id: str, branch: Branch,
        base_data: Mapping[str, Any], branch_data: Mapping[str, Any],
        head_data: Mapping[str, Any],
    ) -> Conflict:
        tokens = self._conflict_path_tokens(key)
        base_value = self._read_conflict_path(base_data, tokens)
        ours_value = self._read_conflict_path(branch_data, tokens)
        theirs_value = self._read_conflict_path(head_data, tokens)
        root_key = tokens[0]
        head_agent, head_intent = self._last_writer(root_key)

        def side(value: Any, *, agent: Optional[str] = None,
                 intent: Optional[str] = None) -> Side:
            present = value is not _MISSING
            return Side(
                present=present,
                hash=content_hash(value) if present else None,
                value=copy.deepcopy(value) if present else None,
                agent_id=agent,
                intent=intent,
            )

        return Conflict(
            key=key,
            base=side(base_value),
            ours=side(ours_value, agent=agent_id, intent=branch.intents.get(root_key)),
            theirs=side(theirs_value, agent=head_agent, intent=head_intent),
        )

    def _last_writer(self, key: str) -> Tuple[Optional[str], Optional[str]]:
        for event in reversed(self._events):
            if event.action == "workspace.commit" and key in event.payload.get("changes", {}):
                return event.agent_id, event.payload.get("intents", {}).get(key)
        return None, None

    def _decide(self, agent_id: str, branch: Branch, head: Snapshot, resolver: Resolver,
                conflicts: List[Conflict]) -> Dict[str, Resolution]:
        """Interroge le résolveur (hors verrou). Annule proprement si non résolu ou invalide."""
        decisions: Dict[str, Resolution] = {}
        unresolved: List[str] = []
        all_keys = [c.key for c in conflicts]
        for conflict in conflicts:
            try:
                decision = resolver.resolve(conflict)
                if decision is not None:
                    if not isinstance(decision, Resolution):
                        raise ResolutionError(f"le résolveur doit rendre une Resolution ou None (reçu {type(decision).__name__})")
                    if decision.present:
                        content_hash(decision.value)  # valide tout de suite : JSON strict
            except Exception as exc:  # noqa: BLE001 - tout échec de résolveur annule le commit
                with self._lock:
                    self._abort(agent_id, branch, head, all_keys, [conflict.key], resolver.name,
                                "resolver_error", error=repr(exc))
                if isinstance(exc, ResolutionError):
                    raise
                raise ResolutionError(
                    f"résolveur '{resolver.name}' en échec sur '{conflict.key}' : {exc!r}",
                    details={"key": conflict.key, "resolver": resolver.name, "cause": repr(exc)},
                ) from exc
            if decision is None:
                unresolved.append(conflict.key)
            else:
                decisions[conflict.key] = decision
        if unresolved:
            with self._lock:
                self._abort(agent_id, branch, head, all_keys, unresolved, resolver.name, "unresolved")
            raise ConflictError(
                f"Conflit non résolu par '{resolver.name}' pour : {unresolved}. Rien n'a été écrasé.",
                conflicting_keys=unresolved,
                code="conflict_unresolved",
                unresolvable_keys=unresolved,
                resolver=resolver.name,
                branch_name=branch.name,
                head_version=head.version_id,
            )
        return decisions

    def _abort(self, agent_id: str, branch: Branch, head: Snapshot, conflicting: List[str],
               unresolved: List[str], resolver_name: Optional[str], reason: str, **extra: Any) -> None:
        """Tout conflit finit par une résolution OU un abandon : jamais de silence."""
        self._events.append(CerbereCohortEvent(
            agent_id=agent_id, action="coordination.aborted",
            payload={"branch_name": branch.name, "base_version": branch.base_version,
                     "head_version": head.version_id, "conflicting_keys": conflicting,
                     "unresolved_keys": unresolved, "resolver": resolver_name, "reason": reason, **extra},
        ))

    def _finalize(self, agent_id: str, branch: Branch, base: Snapshot, head: Snapshot,
                  branch_entries: Mapping[str, str], merged: Mapping[str, str],
                  decisions: Dict[str, Resolution], resolver_name: Optional[str]) -> Snapshot:
        """Crée le snapshot (sous verrou, HEAD vérifié inchangé) et journalise."""
        entries = dict(merged)
        for value in branch.current_data.values():
            self._store.put(value)

        resolutions: Dict[str, Optional[str]] = {}
        for key, decision in sorted(decisions.items()):
            digest: Optional[str] = self._store.put(decision.value) if decision.present else None
            # The resolved value was already applied to the merged JSON tree by
            # _apply_resolution_path. Record only its hash here; replay verifies it.
            resolutions[key] = digest
            self._events.append(CerbereCohortEvent(
                agent_id=agent_id, action="coordination.resolved",
                payload={"branch_name": branch.name, "key": key, "resolver": resolver_name,
                         "choice_hash": digest, "rationale": decision.rationale,
                         "base_version": branch.base_version, "head_version": head.version_id},
            ))

        parents = (head.version_id,) if branch.base_version == head.version_id \
            else (head.version_id, branch.base_version)
        snapshot = _make_snapshot(entries, parents, self._store)
        self._snapshots[snapshot.version_id] = snapshot
        self._head_version = snapshot.version_id

        changes = {key: entries.get(key) for key in sorted(set(entries) | set(head.entries))
                   if entries.get(key) != head.entries.get(key)}
        self._events.append(CerbereCohortEvent(
            agent_id=agent_id, action="workspace.commit",
            payload={
                "version_id": snapshot.version_id,
                "parent_version": parents[0],
                "parents": list(parents),
                "merge_type": "resolved" if resolutions else ("fast-forward" if len(parents) == 1 else "auto-merge"),
                "keys_modified": sorted(k for k in set(base.entries) | set(branch_entries)
                                        if base.entries.get(k) != branch_entries.get(k)),
                "changes": changes,        # clé -> hash du nouveau contenu, ou null si supprimée
                "resolutions": resolutions,  # décisions appliquées : le replay les VÉRIFIE
                "intents": {k: branch.intents[k] for k in changes if k in branch.intents and k not in resolutions},
            },
        ))
        return snapshot

    def seed(self, data: Dict[str, Any], agent_id: str = "system") -> Snapshot:
        """Initialise l'état par un vrai commit (journalisé, rejouable). Ignore les leases."""
        branch = self.create_branch(agent_id, "seed")
        # Seed is a trusted system initialization path, not an agent write.
        branch._lease_exempt = True
        for key, value in data.items():
            branch._put(key, value)
        return self.commit(agent_id, branch)

    # --- Replay -----------------------------------------------------------------

    @classmethod
    def replay(cls, events: Iterable[CerbereCohortEvent], store: ContentStore,
               require_leases: bool = False) -> "Workspace":
        """Reconstruit l'état depuis le journal et VÉRIFIE chaque id de snapshot.

        Seuls les événements `workspace.commit` modifient l'état ; les autres sont
        recopiés tels quels dans le journal du workspace reconstruit. Les agents ne
        sont pas ré-exécutés : on rejoue l'état, pas le comportement (CONCEPTS §7).
        """
        ws = cls(require_leases=require_leases, store=store)
        for event in events:
            ws._events.append(event)
            if event.action != "workspace.commit":
                continue
            p = event.payload
            if "changes" not in p or "parents" not in p:
                raise ReplayError(
                    f"événement {event.event_id} : format antérieur au replay (champs 'changes'/'parents' absents)"
                )
            parents = tuple(p["parents"])
            if not parents or parents[0] != ws._head_version:
                raise ReplayError(
                    f"chaîne rompue : le commit {str(p.get('version_id'))[:12]} attend le parent "
                    f"{str(parents[:1])} mais HEAD est {ws._head_version[:12]}"
                )
            if any(parent not in ws._snapshots for parent in parents):
                raise ReplayError(f"parent inconnu dans {list(parents)}")
            entries = dict(ws.head.entries)
            for key, digest in p["changes"].items():
                if digest is None:
                    entries.pop(key, None)
                elif store.has(digest):
                    entries[key] = digest
                else:
                    raise ReplayError(f"objet {digest[:12]} manquant pour la clé '{key}'")
            snapshot = _make_snapshot(entries, parents, store)
            for key, digest in p.get("resolutions", {}).items():
                tokens = cls._conflict_path_tokens(key)
                resolved_value = cls._read_conflict_path(snapshot.data, tokens)
                if digest is None:
                    matches = resolved_value is _MISSING
                else:
                    matches = (
                        resolved_value is not _MISSING
                        and content_hash(resolved_value) == digest
                    )
                if not matches:
                    raise ReplayError(f"décision enregistrée incohérente pour '{key}' dans {event.event_id}")
            if snapshot.version_id != p["version_id"]:
                raise ReplayError(
                    f"id divergent : journal={str(p['version_id'])[:12]} reconstruit={snapshot.version_id[:12]}"
                )
            ws._snapshots[snapshot.version_id] = snapshot
            ws._head_version = snapshot.version_id
        return ws

    # --- Leases -----------------------------------------------------------------

    def acquire_lease(self, agent_id: str, resource_key: str, duration_seconds: float = 30.0, **kwargs) -> Any:
        """Acquiert un verrou (kwargs : wait, timeout_seconds)."""
        return self._lease_manager.acquire(agent_id, resource_key, duration_seconds, **kwargs)

    def release_lease(self, agent_id: str, resource_key: str) -> None:
        """Libère un verrou sur une ressource."""
        self._lease_manager.release(agent_id, resource_key)

    def get_events(self) -> List[CerbereCohortEvent]:
        """Retourne tous les événements (workspace + leases), triés par timestamp (tri stable)."""
        all_events = self._events.copy()
        all_events.extend(self._lease_manager.get_events())
        all_events.sort(key=lambda e: e.timestamp)
        return all_events
