"""Small, process-local security primitives shared by the application layers.

This module deliberately has no Streamlit dependency.  The same authorization
and locking rules can therefore be used by UI handlers, background workers and
deployment-side tooling without trusting browser state.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from threading import BoundedSemaphore, Lock
import time
from uuid import uuid4


LOGGER = logging.getLogger("feutsolver.security")
ACTOR_HEADER = "x-authenticated-user"
_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[^\s,;]+"), r"\1[redacted]"),
    (re.compile(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]+"), "[redacted]"),
)
_ALTERNATIVE_IDENTITY_HEADERS = {
    "remote-user",
    "x-forwarded-user",
    "x-remote-user",
    "x-auth-user",
    "x-user",
}
_REDACTED_DETAIL_MARKERS = {
    "authorization", "cookie", "header", "password", "secret", "api_key", "apikey",
    "token", "payload", "body", "image", "screenshot", "session",
}


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


APP_ENV = os.getenv("APP_ENV", "production").strip().lower() or "production"
ALLOW_LOCAL_MUTATIONS = (
    APP_ENV in {"development", "test"}
    and os.getenv("APP_ALLOW_LOCAL_WORDLIST_MUTATIONS", "0").strip().lower()
    in {"1", "true", "yes"}
)

OCR_CONCURRENCY = _positive_int("FEUTSOLVER_MAX_OCR_WORKERS", 2, 16)
SOLVER_CONCURRENCY = _positive_int("FEUTSOLVER_MAX_SOLVER_WORKERS", 2, 16)
_RESOURCE_SEMAPHORES = {
    "ocr": BoundedSemaphore(OCR_CONCURRENCY),
    "solver": BoundedSemaphore(SOLVER_CONCURRENCY),
}


def _configure_logging() -> None:
    """Ensure security events have a real server-side destination."""
    root = logging.getLogger()
    # Keep unrelated library loggers quiet.  The security logger below is
    # deliberately raised to INFO, so audit events remain available without
    # turning urllib3, PIL, Tornado, and similar dependencies into audit noise.
    root.setLevel(logging.WARNING)
    if not root.handlers:
        logging.basicConfig(
            level=logging.WARNING,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    LOGGER.setLevel(logging.INFO)


_configure_logging()


class ResourceLimitError(RuntimeError):
    """A bounded resource or request budget could not be acquired."""

    def __init__(self, code: str, message: str = "resource limit reached") -> None:
        super().__init__(message)
        self.code = code


@contextmanager
def resource_slot(resource: str, *, timeout_seconds: float = 0.25) -> Iterator[None]:
    """Acquire one process-wide bounded OCR/solver slot, or fail closed."""
    semaphore = _RESOURCE_SEMAPHORES.get(resource)
    if semaphore is None:
        raise ValueError(f"unknown resource: {resource}")
    acquired = semaphore.acquire(timeout=max(0.01, timeout_seconds))
    if not acquired:
        raise ResourceLimitError(
            f"{resource.upper()}-BUSY",
            f"too many concurrent {resource} tasks",
        )
    try:
        yield
    finally:
        semaphore.release()


def authenticated_actor(headers: Mapping[str, object] | None) -> str | None:
    """Return exactly one trusted-looking actor header, otherwise ``None``.

    Apache must remove client-supplied identity headers before setting the
    canonical header.  This parser additionally rejects duplicate/case-variant
    canonical headers, alternate identity headers, commas and control chars.
    """
    if headers is None:
        return None
    canonical: list[object] = []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        try:
            canonical = list(get_all(ACTOR_HEADER))
        except (TypeError, ValueError, KeyError):
            return None
    for key, value in headers.items():
        folded = str(key).strip().casefold()
        if folded == ACTOR_HEADER:
            # StreamlitHeaders exposes repeated values through get_all().
            if not callable(get_all):
                canonical.append(value)
        elif folded in _ALTERNATIVE_IDENTITY_HEADERS:
            return None
    if len(canonical) != 1:
        return None
    raw = canonical[0]
    if not isinstance(raw, str):
        return None
    actor = raw.strip()
    if not actor or actor != raw or not _ACTOR_PATTERN.fullmatch(actor):
        return None
    return actor


def may_mutate_production_wordlist(actor: str | None) -> bool:
    """Authorize the only production wordlist writer.

    The development bypass is intentionally separate from actor identity and
    requires both an explicit non-production environment and an opt-in flag.
    """
    if actor == "feutsolver":
        return True
    return ALLOW_LOCAL_MUTATIONS and actor is None


def new_correlation_id() -> str:
    return uuid4().hex


def redact_text(value: object, *, maximum: int = 600) -> str:
    """Bound and redact exception text before it reaches server-side logs."""
    text = " ".join(str(value).split())
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def log_security_event(
    *,
    event: str,
    correlation_id: str,
    actor: str | None = None,
    action: str | None = None,
    result: str | None = None,
    duration_ms: float | None = None,
    error_category: str | None = None,
    **details: object,
) -> None:
    """Emit a redaction-safe structured event.

    Callers must pass categories and counters, not request bodies, headers,
    cookies, credentials or OCR/API payloads.
    """
    payload: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        "correlation_id": correlation_id,
        "actor": actor,
        "action": action,
        "result": result,
    }
    if duration_ms is not None:
        payload["duration_ms"] = round(max(0.0, duration_ms), 2)
    if error_category is not None:
        payload["error_category"] = error_category
    for key, value in details.items():
        folded_key = str(key).casefold().replace("-", "_")
        if any(marker in folded_key for marker in _REDACTED_DETAIL_MARKERS):
            payload[key] = "[redacted]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = redact_text(value) if isinstance(value, str) else value
        else:
            payload[key] = f"<{type(value).__name__}>"
    LOGGER.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def file_version(path: Path) -> str | None:
    """Return a content digest for audit/cache identity, without reading huge files twice."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def audit_wordlist_mutation(
    *,
    actor: str | None,
    action: str,
    word_count: int,
    result: str,
    correlation_id: str,
    version_before: str | None,
    version_after: str | None,
) -> None:
    """Write the required audit fields and no sensitive request material."""
    log_security_event(
        event="wordlist_mutation",
        correlation_id=correlation_id,
        actor=actor,
        action=action,
        result=result,
        word_count=word_count,
        version_before=version_before,
        version_after=version_after,
    )


class SlidingWindowRateLimiter:
    """Bounded in-process sliding-window limiter for external provider calls."""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 2048) -> None:
        self.limit = max(1, limit)
        self.window_seconds = max(1.0, window_seconds)
        self.max_keys = max(1, max_keys)
        self._entries: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            if key not in self._entries and len(self._entries) >= self.max_keys:
                oldest_key = min(self._entries, key=lambda item: self._entries[item][-1])
                del self._entries[oldest_key]
            entries = self._entries.setdefault(key, deque())
            cutoff = current - self.window_seconds
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self.limit:
                return False
            entries.append(current)
            return True


@dataclass
class OCRBudget:
    """Per-upload deadline and request-attempt budget."""

    deadline: float
    max_attempts: int
    attempts: int = 0

    @classmethod
    def start(cls, *, timeout_seconds: float, max_attempts: int) -> "OCRBudget":
        return cls(
            deadline=time.monotonic() + max(0.1, timeout_seconds),
            max_attempts=max(1, max_attempts),
        )

    def check(self, code: str = "OCR-TIMEOUT") -> None:
        if time.monotonic() >= self.deadline:
            raise ResourceLimitError(code, "OCR deadline exceeded")

    def remaining_seconds(self) -> float:
        """Return the remaining wall-clock budget for one bounded operation."""
        return max(0.0, self.deadline - time.monotonic())

    def consume_attempt(self) -> None:
        self.check("OCR-TIMEOUT")
        if self.attempts >= self.max_attempts:
            raise ResourceLimitError("OCR-ATTEMPTS", "OCR attempt budget exceeded")
        self.attempts += 1
