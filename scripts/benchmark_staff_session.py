"""Measure local staff-session reads at the approved NFR1.1 load.

This standalone harness is intentionally outside pytest collection. It mutates only the
explicit FINANCE_AI_TEST_DATABASE_URL, which must name a loopback PostgreSQL database
ending in ``_test``. Run the API separately with FINANCE_AI_DATABASE_URL pointed at the
same database and FINANCE_AI_ENVIRONMENT=test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--mode",
        choices=("staff", "baseline-mobile"),
        default="staff",
        help="Measure staff /session or baseline mobile /auth/me (not directly comparable).",
    )
    parser.add_argument("--duration-seconds", type=int, default=300)
    return parser.parse_args()


def _validated_test_url():
    from sqlalchemy.engine import make_url

    raw_url = os.environ.get("FINANCE_AI_TEST_DATABASE_URL")
    if not raw_url:
        raise SystemExit("FINANCE_AI_TEST_DATABASE_URL must identify a disposable _test database")
    url = make_url(raw_url)
    if not (url.database or "").endswith("_test"):
        raise SystemExit("Refusing to use a database whose name does not end with _test")
    if (url.host or "").casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Refusing to connect to a non-loopback database host")
    os.environ["FINANCE_AI_DATABASE_URL"] = raw_url
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    return url


def _ensure_schema_and_seed() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    command.upgrade(Config(str(root / "alembic.ini")), "head")
    from finance_ai.db.seeds import seed_shared_data
    from finance_ai.db.session import clear_session_caches, get_session_factory

    clear_session_caches()
    with get_session_factory()() as session, session.begin():
        seed_shared_data(session)


def _seed_staff_accounts(count: int) -> tuple[list[tuple[str, str]], list[int]]:
    from sqlalchemy import select

    from finance_ai.auth.dependencies import get_password_hasher
    from finance_ai.db.models.admin import StaffInvitation
    from finance_ai.db.models.identity import AppUser, Role
    from finance_ai.db.models.reference import Currency
    from finance_ai.db.session import get_session_factory

    now = datetime.now(UTC)
    password = secrets.token_urlsafe(32)
    password_hash = get_password_hasher().hash(password)
    created: list[tuple[str, str]] = []
    user_ids: list[int] = []
    with get_session_factory()() as session, session.begin():
        admin_role = session.scalar(select(Role).where(Role.name == "admin"))
        support_role = session.scalar(select(Role).where(Role.name == "support_agent"))
        currency = session.scalar(select(Currency).where(Currency.code == "COP"))
        if admin_role is None or support_role is None or currency is None:
            raise RuntimeError("Required test reference data is missing")
        admin = AppUser(
            email=f"staff-benchmark-admin-{secrets.token_hex(8)}@example.invalid",
            password_hash=password_hash,
            role_id=admin_role.id,
            preferred_currency_id=currency.id,
        )
        session.add(admin)
        session.flush()
        for index in range(count):
            email = f"staff-benchmark-{index}-{secrets.token_hex(8)}@example.invalid"
            user = AppUser(
                email=email,
                password_hash=password_hash,
                role_id=support_role.id,
                preferred_currency_id=currency.id,
            )
            session.add(user)
            session.flush()
            session.add(
                StaffInvitation(
                    email=email,
                    role_id=support_role.id,
                    token_hash=secrets.token_hex(32),
                    created_by_admin_user_id=admin.id,
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    consumed_at=now,
                    activated_user_id=user.id,
                )
            )
            created.append((email, password))
            user_ids.append(user.id)
    return created, user_ids


def _seed_baseline_accounts(count: int, base_url: str) -> list[tuple[str, str]]:
    del base_url
    from sqlalchemy import select

    from finance_ai.auth.credentials import CredentialCodec
    from finance_ai.db.models.identity import AppUser, Role
    from finance_ai.db.models.reference import Currency
    from finance_ai.db.models.sessions import UserSession
    from finance_ai.db.session import get_session_factory

    now = datetime.now(UTC)
    access_expiry = now + timedelta(minutes=15)
    idle_expiry = now + timedelta(minutes=30)
    absolute_expiry = now + timedelta(hours=8)
    accounts: list[tuple[str, str]] = []
    with get_session_factory()() as session, session.begin():
        role = session.scalar(select(Role).where(Role.name == "user"))
        currency = session.scalar(select(Currency).where(Currency.code == "COP"))
        if role is None or currency is None:
            raise RuntimeError("Baseline test reference data is missing")
        for index in range(count):
            email = f"staff-benchmark-baseline-{index}-{secrets.token_hex(8)}@example.invalid"
            user = AppUser(
                email=email,
                password_hash=secrets.token_hex(32),
                role_id=role.id,
                preferred_currency_id=currency.id,
            )
            session.add(user)
            session.flush()
            access = CredentialCodec().issue()
            session.add(
                UserSession(
                    user_id=user.id,
                    access_token_hash=access.token_hash,
                    platform="android",
                    access_expires_at=access_expiry,
                    idle_expires_at=idle_expiry,
                    absolute_expires_at=absolute_expiry,
                    last_renewed_at=now,
                )
            )
            accounts.append((email, access.raw))
    return accounts


async def _run_load(
    base_url: str,
    mode: str,
    accounts: list[tuple[str, str]],
    *,
    duration_seconds: int,
    requests_per_second: int = 5,
) -> dict[str, object]:
    import httpx

    parsed_base_url = urlsplit(base_url)
    origin = f"{parsed_base_url.scheme}://{parsed_base_url.hostname}:5173"
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    timeout = httpx.Timeout(10.0, connect=3.0)
    clients = [
        httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout) for _ in accounts
    ]
    credentials: list[str | None] = []
    try:
        for client, (email, password) in zip(clients, accounts, strict=True):
            if mode == "staff":
                await _login_staff_account(client, email, password, origin)
                credentials.append(None)
            else:
                credentials.append(password)

        endpoint = "/api/v1/staff/auth/session" if mode == "staff" else "/api/v1/auth/me"
        latencies_ms: list[float] = []
        statuses: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        total = duration_seconds * requests_per_second
        started = time.perf_counter()

        async def request(index: int) -> None:
            client_index = index % len(clients)
            headers = (
                {"Origin": origin, "X-Staff-Request": "1"}
                if mode == "staff"
                else {"Authorization": f"Bearer {credentials[client_index]}"}
            )
            before = time.perf_counter()
            try:
                response = await clients[client_index].get(endpoint, headers=headers)
                statuses[str(response.status_code)] += 1
                latencies_ms.append((time.perf_counter() - before) * 1000)
                if response.status_code != 200:
                    errors[f"http_{response.status_code}"] += 1
            except httpx.TimeoutException:
                latencies_ms.append((time.perf_counter() - before) * 1000)
                errors["timeout"] += 1
            except httpx.HTTPError as exc:
                latencies_ms.append((time.perf_counter() - before) * 1000)
                errors[type(exc).__name__] += 1

        tasks: list[asyncio.Task[None]] = []
        for index in range(total):
            due = started + index / requests_per_second
            wait = due - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            tasks.append(asyncio.create_task(request(index)))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        ordered = sorted(latencies_ms)

        def percentile(value: float) -> float:
            if not ordered:
                return 0.0
            rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * value)))
            return round(ordered[rank], 2)

        return {
            "path": endpoint,
            "requests": len(latencies_ms),
            "elapsed_seconds": round(elapsed, 2),
            "achieved_requests_per_second": round(len(latencies_ms) / elapsed, 3),
            "status_counts": dict(statuses),
            "errors": dict(errors),
            "latency_ms": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
                "max": round(max(ordered), 2) if ordered else 0.0,
            },
            "nfr1_1_budget_ms": {"p95": 300, "p99": 750},
        }
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


async def _login_staff_account(client, email: str, password: str, origin: str) -> None:
    """Create one benchmark session, waiting when the API throttles setup logins."""
    max_attempts = 5
    for attempt in range(max_attempts):
        response = await client.post(
            "/api/v1/staff/auth/login",
            json={"email": email, "password": password},
            headers={"Origin": origin, "X-Staff-Request": "1"},
        )
        if response.status_code != 429 or attempt == max_attempts - 1:
            break
        try:
            retry_after = max(1, int(response.headers.get("Retry-After", "1")))
        except ValueError:
            retry_after = 1
        await asyncio.sleep(retry_after + 0.1)

    if response.status_code != 200:
        raise RuntimeError(f"Staff login setup failed with HTTP {response.status_code}")


def main() -> int:
    args = _arguments()
    if args.duration_seconds < 1:
        raise SystemExit("duration-seconds must be positive")
    base = urlsplit(args.base_url)
    if base.scheme not in {"http", "https"} or not base.hostname:
        raise SystemExit("base-url must be an HTTP(S) URL")
    if base.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Refusing to load-test a non-loopback API host")

    test_url = _validated_test_url()
    _ensure_schema_and_seed()
    if args.mode == "staff":
        accounts, _user_ids = _seed_staff_accounts(20)
    else:
        accounts = _seed_baseline_accounts(20, args.base_url)
    result = asyncio.run(
        _run_load(
            args.base_url,
            args.mode,
            accounts,
            duration_seconds=args.duration_seconds,
        )
    )
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except OSError, subprocess.CalledProcessError:
        revision = "unknown"
    report = {
        "benchmark": "STAFF-01 NFR1.1 local session-read regression",
        "mode": args.mode,
        "baseline_comparable": False,
        "comparable_to_nfr1_1_target": args.mode == "staff",
        "comparison_note": (
            "The unchanged develop checkout has no staff browser-session endpoint; its existing "
            "mobile /auth/me measurement is a reference only and is not a direct comparison."
            if args.mode == "baseline-mobile"
            else "Feature staff /session path measured against the same local test setup."
        ),
        "git_revision": revision,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "api_origin": f"{base.scheme}://{base.netloc}",
        "database_name": test_url.database,
        "concurrent_sessions": 20,
        "target_requests_per_second": 5,
        "duration_seconds": args.duration_seconds,
        "startup_and_session_creation_excluded": True,
        **result,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
