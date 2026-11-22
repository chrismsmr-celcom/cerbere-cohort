"""Three-way JSON merge with path-level conflict detection.

Objects (JSON dictionaries) merge recursively. Arrays and scalar values are
atomic: concurrent divergent edits to the same value conflict rather than
being guessed or reordered.
"""
from base64 import urlsafe_b64encode
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .errors import CerbereError
from .hashing import canonical_json

_ABSENT = object()


def _clone(value: Any) -> Any:
    """Copy JSON values without cloning the identity-based absence sentinel."""
    return _ABSENT if value is _ABSENT else deepcopy(value)


def _same(left: Any, right: Any) -> bool:
    """Compare JSON values using canonical types, not Python's bool/int equality."""
    if left is _ABSENT or right is _ABSENT:
        return left is right
    return canonical_json(left) == canonical_json(right)


class ConflictError(CerbereError):
    """Raised when concurrent changes diverge on the same JSON path."""

    code = "conflict_detected"

    def __init__(
        self,
        message: str = "",
        conflicting_keys: Optional[List[str]] = None,
        code: Optional[str] = None,
        **details: Any,
    ):
        self.conflicting_keys: List[str] = list(conflicting_keys or [])
        details.setdefault("conflicting_keys", list(self.conflicting_keys))
        super().__init__(message, details=details, code=code)


@dataclass
class MergeResult:
    success: bool
    merged_data: Optional[Dict[str, Any]] = None
    conflicting_keys: Optional[List[str]] = None


def _conflict_path(tokens: Tuple[str, ...]) -> str:
    """Keep legacy root-key names; encode nested paths as JSON Pointers."""
    if len(tokens) == 1:
        key = tokens[0]
        # Reserve both path prefixes. Root keys using either prefix are encoded
        # so they can never be mistaken for nested JSON Pointer conflicts.
        if key.startswith(("json-pointer:/", "root-key:")):
            encoded = urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")
            return "root-key:v1:" + encoded
        return key
    escaped = (part.replace("~", "~0").replace("/", "~1") for part in tokens)
    return "json-pointer:/" + "/".join(escaped)


def _merge_node(
    base: Any,
    mine: Any,
    head: Any,
    path: Tuple[str, ...],
    conflicts: List[str],
) -> Any:
    """Merge one JSON node, returning the merged value or _ABSENT."""
    if _same(mine, base):
        return _clone(head)
    if _same(head, base) or _same(head, mine):
        return _clone(mine)

    # Recurse only through JSON objects. Arrays remain atomic because index
    # merges can silently reorder or combine semantically related elements.
    base_is_object = base is _ABSENT or isinstance(base, dict)
    if base_is_object and isinstance(mine, dict) and isinstance(head, dict):
        base_obj = {} if base is _ABSENT else base
        merged: Dict[str, Any] = {}
        for key in sorted(set(base_obj) | set(mine) | set(head)):
            value = _merge_node(
                base_obj.get(key, _ABSENT),
                mine.get(key, _ABSENT),
                head.get(key, _ABSENT),
                path + (key,),
                conflicts,
            )
            if value is not _ABSENT:
                merged[key] = value
        return merged

    conflicts.append(_conflict_path(path))
    return _clone(head)


def detect_and_merge(
    base_data: Dict[str, Any],
    branch_data: Dict[str, Any],
    current_head_data: Dict[str, Any],
) -> MergeResult:
    """Perform a recursive three-way merge of JSON objects.

    - unchanged branch values keep HEAD;
    - unchanged HEAD values accept the branch;
    - identical concurrent values are accepted;
    - dictionaries merge recursively by key;
    - arrays and scalars are atomic;
    - delete-vs-modify and divergent edits produce a conflict.

    Root-level conflict names remain unchanged for API compatibility.
    Nested conflict names use `json-pointer:/root/nested/path` with RFC 6901
    escaping for `~` and `/`.
    """
    merged: Dict[str, Any] = {}
    conflicts: List[str] = []

    for key in sorted(set(base_data) | set(branch_data) | set(current_head_data)):
        value = _merge_node(
            base_data.get(key, _ABSENT),
            branch_data.get(key, _ABSENT),
            current_head_data.get(key, _ABSENT),
            (key,),
            conflicts,
        )
        if value is not _ABSENT:
            merged[key] = value

    if conflicts:
        return MergeResult(
            success=False,
            merged_data=merged,
            conflicting_keys=sorted(set(conflicts)),
        )
    return MergeResult(success=True, merged_data=merged)
