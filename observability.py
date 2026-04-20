"""
Phase 5 hardening — observability, rate limiting, and structured error
emails.

Spec references:
  * §13.3 "Things I'd like to add in hardening":
      - /healthz that checks DB + xlsx + email client
      - rate limit on /api/auth/magic-link (3 requests / 5 minutes per email)
      - structured error emails to admin on 5xx
  * §12 Phase 5: "Hardening — backups, audit coverage, error handling,
    basic observability (access log + errors to email), polish pass"

Intentionally in-process / in-memory: Somm v2.0 runs as a single gunicorn
worker on Render (SQLite forces single-writer) so a per-process deque is
sufficient for 10–20 users. When we outgrow this, swap the rate-limiter
for Redis and the exception emailer for a real APM — the public surface
(RateLimiter.check, send_error_email, healthz_snapshot) stays the same.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

log = logging.getLogger("somm")
if not log.handlers:
    # Render captures stdout; keep formatting cheap and line-oriented.
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Rate limiter — per-key sliding window
# ---------------------------------------------------------------------------


class RateLimiter:
    """
    Per-key sliding-window limiter. `check(key)` returns (allowed, retry_after).
    Thread-safe enough for a single gunicorn worker; we hold the lock only for
    the O(window) deque trim.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            # Evict anything older than the window.
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                retry_after = max(0.0, self.window - (now - dq[0]))
                return False, retry_after
            dq.append(now)
            return True, 0.0

    def reset(self, key: Optional[str] = None) -> None:
        """Used only by tests — never in production paths."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Singleton for /api/auth/magic-link — 3 requests per email per 5 minutes.
# Spec §13.3 chose this specifically to prevent nuisance floods while still
# letting a real user retry if their first link gets lost in spam.
magic_link_rate_limiter = RateLimiter(max_requests=3, window_seconds=300)


# ---------------------------------------------------------------------------
# Access log middleware
# ---------------------------------------------------------------------------


async def access_log_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Any]]
):
    """
    One-line access log per request: method path -> status (duration ms)
    user=<id-or-anon>. We deliberately do NOT log query strings or bodies
    (both can contain tokens or PII). User id is captured best-effort from
    request.state if an auth dependency set it; otherwise 'anon'.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # The 5xx handler below will record the exception + email admin;
        # still emit an access-log row for the failed request.
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.info(
            "%s %s -> 500 (%.1f ms) user=unknown [exception]",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        user_obj = getattr(request.state, "user", None)
        user_id = getattr(user_obj, "id", None) or "anon"
    log.info(
        "%s %s -> %d (%.1f ms) user=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        user_id,
    )
    return response


# ---------------------------------------------------------------------------
# 5xx handler — log + email admin
# ---------------------------------------------------------------------------


def _admin_error_recipients() -> list[str]:
    """
    Comma-separated list of emails that receive 5xx alerts. Falls back to
    ADMIN_EMAIL (the bootstrap founder account) if unset.
    """
    raw = os.environ.get("ADMIN_ALERT_EMAILS", "") or os.environ.get("ADMIN_EMAIL", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def send_error_email(subject: str, body: str) -> None:
    """
    Best-effort email to the admin about an uncaught 5xx. Uses the same
    Resend client as magic-link mail. Swallows all failures — the request
    is already returning 500 to the user, so failing to email admin must
    not cascade.
    """
    try:
        import auth  # Reuse the configured Resend client.
    except Exception:
        return
    if not auth.RESEND_API_KEY:
        log.warning("5xx alert skipped (no RESEND_API_KEY): %s", subject)
        return
    recipients = _admin_error_recipients()
    if not recipients:
        log.warning("5xx alert skipped (no ADMIN_ALERT_EMAILS / ADMIN_EMAIL): %s", subject)
        return
    try:
        import resend  # type: ignore

        resend.Emails.send(
            {
                "from": auth.MAGIC_LINK_FROM if hasattr(auth, "MAGIC_LINK_FROM") else "Somm <onboarding@resend.dev>",
                "to": recipients,
                "subject": subject,
                "text": body,
            }
        )
    except Exception as e:
        log.warning("5xx alert email failed: %s", e)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global 5xx handler. Logs with traceback, emails the admin, and returns
    a generic error body — we never leak stack frames or internal detail
    to the HTTP client.
    """
    tb = traceback.format_exc()
    log.error(
        "Unhandled exception on %s %s: %s\n%s",
        request.method,
        request.url.path,
        exc,
        tb,
    )

    ts = datetime.now(timezone.utc).isoformat()
    user_id = getattr(request.state, "user_id", None) or "anon"
    subject = f"[Somm] 5xx on {request.method} {request.url.path}"
    body = (
        f"Time: {ts}\n"
        f"Request: {request.method} {request.url.path}\n"
        f"User: {user_id}\n"
        f"Exception: {exc!r}\n\n"
        f"Traceback:\n{tb}\n"
    )
    send_error_email(subject, body)

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. The admin has been notified."},
    )


# ---------------------------------------------------------------------------
# Health check — DB + xlsx + email client
# ---------------------------------------------------------------------------


async def healthz_snapshot() -> dict:
    """
    Shallow health probe. Each check reports pass/fail/skip independently
    so admin can see which subsystem is degraded. This is the data source
    for /healthz and the admin page's system-status panel.
    """
    checks: dict[str, dict] = {}

    # DB check — run a cheap SELECT 1 against the configured engine.
    try:
        from db import async_session_factory

        async with async_session_factory() as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        checks["db"] = {"status": "ok"}
    except Exception as e:
        checks["db"] = {"status": "fail", "detail": str(e)}

    # Chartier xlsx check — presence + readability, not content.
    try:
        import chartier_sync

        xlsx = chartier_sync.xlsx_path()
        if xlsx.exists() and xlsx.is_file():
            checks["chartier_xlsx"] = {"status": "ok", "path": str(xlsx)}
        else:
            checks["chartier_xlsx"] = {
                "status": "missing",
                "path": str(xlsx),
                "detail": "xlsx file not found at configured path",
            }
    except Exception as e:
        checks["chartier_xlsx"] = {"status": "fail", "detail": str(e)}

    # Email client — just verify the API key is configured. We don't
    # send a live probe (that would cost + spam our sender reputation).
    try:
        import auth

        if auth.RESEND_API_KEY:
            checks["email"] = {"status": "ok"}
        else:
            checks["email"] = {
                "status": "disabled",
                "detail": "RESEND_API_KEY unset (dev mode — links print to stdout)",
            }
    except Exception as e:
        checks["email"] = {"status": "fail", "detail": str(e)}

    # Admin alert recipients — surface this so a misconfigured prod
    # environment (where 5xx alerts would silently drop) is visible.
    recipients = _admin_error_recipients()
    checks["admin_alerts"] = {
        "status": "ok" if recipients else "unconfigured",
        "recipients_count": len(recipients),
    }

    overall = "ok"
    for name, v in checks.items():
        if v["status"] == "fail":
            overall = "fail"
            break
        if v["status"] in ("missing", "disabled", "unconfigured") and overall == "ok":
            overall = "degraded"

    return {
        "status": overall,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def install(app: FastAPI) -> None:
    """
    Register middleware + exception handler + /healthz route on the app.
    Called once from main.py after all other routers are mounted.
    """
    app.middleware("http")(access_log_middleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/healthz")
    async def healthz():
        snap = await healthz_snapshot()
        # Return 503 when any subsystem is fully broken so load balancers
        # and uptime monitors can distinguish degraded-but-serving from down.
        status_code = 200 if snap["status"] != "fail" else 503
        return JSONResponse(status_code=status_code, content=snap)
