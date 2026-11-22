import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


_current_event_actor: ContextVar[dict[str, Any] | None] = ContextVar(
    "cohort_event_actor", default=None
)


@contextmanager
def event_actor(context: dict[str, Any]) -> Iterator[None]:
    """Attach the authenticated API principal to events emitted in this context."""
    principal_type = context.get("principal_type") or (
        "agent" if context.get("agent_id") else "user"
    )
    principal_id = (
        context.get("agent_id") if principal_type == "agent"
        else context.get("user_id")
    )
    if not principal_id:
        yield
        return
    actor = {
        "type": principal_type,
        "id": str(principal_id),
        "credential_id": context.get("credential_id") if principal_type == "agent" else None,
    }
    token = _current_event_actor.set(actor)
    try:
        yield
    finally:
        _current_event_actor.reset(token)


class CerbereCohortEvent(BaseModel):
    """Immutable event; metadata.actor identifies the authenticated initiator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = Field(
        default="1.0.0",
        description="Version of the serialized event contract",
    )
    event_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique event ID",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 timestamp",
    )
    agent_id: str = Field(..., description="Agent identity associated with the event")
    action: str = Field(..., description="Type of action performed")
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific data",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional context, including authenticated actor attribution",
    )

    @model_validator(mode="before")
    @classmethod
    def _attach_authenticated_actor(cls, value: Any) -> Any:
        actor = _current_event_actor.get()
        # Persisted/replayed events always carry their event_id. Never apply the
        # current request actor to historical records during workspace reload.
        if actor is None or not isinstance(value, dict) or "event_id" in value:
            return value
        value = dict(value)
        metadata = dict(value.get("metadata") or {})
        metadata["actor"] = dict(actor)
        value["metadata"] = metadata
        return value

    def to_jsonl(self) -> str:
        """Serialize one event as a JSONL record."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> "CerbereCohortEvent":
        """Deserialize one event JSONL record; legacy v1.0.0 records remain valid."""
        return cls.model_validate_json(line)
