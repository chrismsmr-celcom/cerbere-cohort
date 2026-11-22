"""Tenant-configurable OTLP/HTTP log export for Cohort coordination events.

Only operational metadata is exported by default. Workspace values, prompts,
credentials and full event payloads are deliberately excluded.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse


_SAFE_PAYLOAD_FIELDS = {
    "branch_name", "base_version", "head_version", "expected_head_version",
    "actual_head_version", "merge_type", "duration_seconds", "expires_at", "fencing_token",
    "reason", "resolver", "choice_hash", "parent_version", "version_id",
}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")


def encryption_key() -> bytes:
    """Derive a stable AES-256 key from the deployment secret."""
    configured = os.getenv("COHORT_EXPORT_ENCRYPTION_KEY", "").strip()
    if not configured:
        raise RuntimeError("COHORT_EXPORT_ENCRYPTION_KEY is not configured")
    if re.fullmatch(r"[0-9a-fA-F]{64}", configured):
        return bytes.fromhex(configured)
    return hashlib.sha256(configured.encode("utf-8")).digest()


def encrypt_headers(headers: dict[str, str]) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    plaintext = json.dumps(headers, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return nonce + AESGCM(encryption_key()).encrypt(nonce, plaintext, b"cohort-otlp-headers-v1")


def decrypt_headers(ciphertext: bytes | memoryview) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = bytes(ciphertext)
    if len(raw) < 29:
        raise ValueError("Invalid encrypted observability credentials")
    plaintext = AESGCM(encryption_key()).decrypt(
        raw[:12], raw[12:], b"cohort-otlp-headers-v1"
    )
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("Invalid observability headers")
    return value


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    if not isinstance(headers, dict) or len(headers) > 10:
        raise ValueError("Provide at most 10 HTTP headers")
    clean: dict[str, str] = {}
    seen: set[str] = set()
    reserved = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("Invalid HTTP header name")
        normalized_name = name.lower()
        if normalized_name in reserved or normalized_name in seen:
            raise ValueError(f"HTTP header {name} cannot be overridden")
        seen.add(normalized_name)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError(f"Invalid value for HTTP header {name}")
        if "\r" in value or "\n" in value:
            raise ValueError("HTTP header values cannot contain newlines")
        clean[name] = value
    return clean


def normalize_otlp_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OTLP endpoint must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("OTLP endpoint cannot contain a query string or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/logs"):
        path = f"{path}/v1/logs" if path else "/v1/logs"
    return parsed._replace(path=path).geturl()


def _safe_attributes(event: dict[str, Any]) -> list[dict[str, Any]]:
    attributes: dict[str, Any] = {
        "cohort.event_id": event.get("event_id"),
        "cohort.agent_id": event.get("agent_id"),
        "cohort.action": event.get("action"),
        "cohort.schema_version": event.get("schema_version", "1.0.0"),
    }
    metadata = event.get("metadata") or {}
    actor = metadata.get("actor") if isinstance(metadata, dict) else None
    if isinstance(actor, dict):
        if actor.get("type"):
            attributes["cohort.actor.type"] = actor["type"]
        if actor.get("id"):
            attributes["cohort.actor.id"] = actor["id"]
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        for key in _SAFE_PAYLOAD_FIELDS:
            value = payload.get(key)
            if isinstance(value, (str, bool, int, float)):
                attributes[f"cohort.{key}"] = value
        resource_key = payload.get("resource_key")
        if isinstance(resource_key, str):
            attributes["cohort.resource_key_hash"] = hashlib.sha256(
                resource_key.encode("utf-8")
            ).hexdigest()[:16]
        for key in ("conflicting_keys", "unresolved_keys"):
            values = payload.get(key)
            if isinstance(values, list):
                attributes[f"cohort.{key.removesuffix('_keys')}_count"] = len(values)
    result = []
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, bool):
            any_value = {"boolValue": value}
        elif isinstance(value, int):
            any_value = {"intValue": str(value)}
        elif isinstance(value, float):
            any_value = {"doubleValue": value}
        else:
            any_value = {"stringValue": str(value)[:2048]}
        result.append({"key": key, "value": any_value})
    return result


def _timestamp_ns(value: str | None) -> str:
    if not value:
        return str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp() * 1_000_000_000))


def build_otlp_payload(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = []
    for event in events:
        action = str(event.get("action") or "cohort.unknown")
        severity = 17 if "aborted" in action else 13 if "conflict" in action else 9
        records.append({
            "timeUnixNano": _timestamp_ns(event.get("timestamp")),
            "severityNumber": severity,
            "severityText": "ERROR" if severity == 17 else "WARN" if severity == 13 else "INFO",
            "body": {"stringValue": f"Cohort coordination event: {action}"},
            "attributes": _safe_attributes(event),
        })
    return {
        "resourceLogs": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "cerbere-cohort"}},
                {"key": "service.version", "value": {"stringValue": "0.1.0"}},
            ]},
            "scopeLogs": [{
                "scope": {"name": "cerbere-cohort", "version": "1.0.0"},
                "logRecords": records,
            }],
        }],
    }


def _assert_public_endpoint(endpoint: str) -> None:
    """Reject localhost/private destinations before sending tenant credentials."""
    host = urlparse(endpoint).hostname or ""
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local")):
        raise ValueError("OTLP endpoint must use a public hostname")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise RuntimeError("OTLP endpoint hostname could not be resolved") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("OTLP endpoint must resolve only to public IP addresses")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_otlp_logs(endpoint: str, headers: dict[str, str],
                   events: Iterable[dict[str, Any]], timeout: float = 5.0) -> None:
    """Send a batch to an OTLP/HTTP logs endpoint; raise on any non-2xx response."""
    normalized = normalize_otlp_endpoint(endpoint)
    _assert_public_endpoint(normalized)
    payload = build_otlp_payload(events)
    if not payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        return
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Cohort-OTLP-Exporter/0.1",
        **validate_headers(headers),
    }
    request = urllib.request.Request(
        normalized, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=request_headers, method="POST",
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"OTLP endpoint returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OTLP endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Unable to reach OTLP endpoint ({type(exc).__name__})") from exc
