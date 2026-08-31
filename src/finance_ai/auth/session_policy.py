from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class SessionTimes:
    access_expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True)
class SessionPolicy:
    access_lifetime: timedelta
    renewable_idle_lifetime: timedelta
    absolute_lifetime: timedelta
    max_active_sessions: int
    terminal_retention: timedelta

    def initial_times(self, now: datetime) -> SessionTimes:
        absolute = now + self.absolute_lifetime
        idle = min(now + self.renewable_idle_lifetime, absolute)
        return SessionTimes(
            access_expires_at=min(now + self.access_lifetime, idle),
            idle_expires_at=idle,
            absolute_expires_at=absolute,
        )

    def renewed_times(self, *, now: datetime, absolute_expires_at: datetime) -> SessionTimes:
        idle = min(now + self.renewable_idle_lifetime, absolute_expires_at)
        return SessionTimes(
            access_expires_at=min(now + self.access_lifetime, idle),
            idle_expires_at=idle,
            absolute_expires_at=absolute_expires_at,
        )

    def cleanup_cutoff(self, now: datetime) -> datetime:
        return now - self.terminal_retention
