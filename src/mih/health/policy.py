"""Stream lifecycle (spec 17): how one observation moves a stream between states.

Pure functions over a small state record, so the rules can be tested without a
network or a database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

VANTAGES = ("server", "home", "device")
# Reasons the health check owns. 'source_absent' belongs to the importer and
# 'policy_excluded' to the user; a probe never sets or clears those.
PROBE_REASONS = ("drm_unsupported", "authentication_required", "blocked_by_geo")

# How long until the next probe, by status, when nothing is going wrong. A working stream
# is looked at once a week; one that started failing hourly until it is back or
# quarantined (quarantine_after_days), then weekly, and archived ones monthly.
RECHECK_MINUTES = {
    "unverified": 0,
    "new": 60,
    "active": 7 * 24 * 60,
    "suspect": 60,
    "quarantine": 7 * 24 * 60,
    "archived": 30 * 24 * 60,
}
# A stream new to the catalog is probed hourly for its first trial_hours: enough checks
# to tell a stable stream from a flapping one and to record its resolution and start-up
# time. After that it falls back to the weekly check.
TRIAL_MINUTES = 60

# What a TV reports when no picture came in time. A slow start (a heavy HD stream, a far
# server, a busy line) looks the same as a dead stream, so it is not held against the
# stream: the server probes it at once and the probe decides.
SLOW_START_ERRORS = ("timeout",)

DEFAULT_CANARIES = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/")


@dataclass(frozen=True)
class HealthPolicy:
    # Where this install's prober sits. On a remote server (spec 14.1) the prober's
    # view differs from the TV's, which is why device reports outrank it.
    vantage: str = "server"
    concurrency: int = 8
    # IPTV providers often allow one connection per account: probing two streams of
    # the same playlist at once can knock the TV off. One at a time per source.
    per_source_concurrency: int = 1
    batch_limit: int = 200
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 10.0
    probe_deadline_s: float = 20.0
    manifest_max_bytes: int = 256 * 1024
    segment_bytes: int = 64 * 1024
    suspect_failures: int = 3
    suspect_windows: int = 2
    window_minutes: int = 60
    trial_hours: int = 24
    quarantine_after_days: int = 3
    archive_after_days: int = 30
    device_trust_hours: int = 72
    observation_retention_days: int = 90
    canary_urls: tuple[str, ...] = DEFAULT_CANARIES

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HealthPolicy:
        env = dict(os.environ if env is None else env)
        vantage = env.get("MIH_HEALTH_VANTAGE", "server")
        if vantage not in ("server", "home"):
            raise ValueError("MIH_HEALTH_VANTAGE must be 'server' or 'home'")
        canaries = env.get("MIH_HEALTH_CANARY_URLS")
        return cls(
            vantage=vantage,
            concurrency=max(1, int(env.get("MIH_HEALTH_CONCURRENCY", "8"))),
            per_source_concurrency=max(1, int(env.get("MIH_HEALTH_PER_SOURCE", "1"))),
            batch_limit=max(1, int(env.get("MIH_HEALTH_BATCH", "200"))),
            device_trust_hours=max(1, int(env.get("MIH_HEALTH_DEVICE_TRUST_HOURS", "72"))),
            canary_urls=DEFAULT_CANARIES if canaries is None else tuple(canaries.split()),
        )


@dataclass(frozen=True)
class StreamState:
    status: str = "unverified"
    availability_reason: str | None = None
    status_changed_at: datetime | None = None
    last_ok_at: datetime | None = None
    first_fail_at: datetime | None = None
    streak_failures: int = 0
    streak_windows: int = 0
    last_fail_window: int | None = None
    last_device_ok_at: datetime | None = None
    last_device_fail_at: datetime | None = None


@dataclass(frozen=True)
class Observation:
    at: datetime
    vantage: str
    success: bool
    # An availability reason the probe established (DRM, auth, geo): the stream is
    # there, but this install cannot play it. Not evidence that it is dead.
    reason: str | None = None
    # Set by the caller when the result is about the path, not the stream.
    uncounted: str | None = None


def is_slow_start(error: str | None) -> bool:
    return (error or "").strip().lower() in SLOW_START_ERRORS


def device_report(at: datetime, ok: bool, error: str | None = None,
                  vantage: str = "device") -> Observation:  # fmt: skip
    """A player's result. A slow start is kept on record but counts neither way."""
    slow = not ok and is_slow_start(error)
    return Observation(at=at, vantage=vantage, success=ok,
                       uncounted="device_slow" if slow else None)  # fmt: skip


@dataclass(frozen=True)
class Decision:
    state: StreamState
    counted: bool
    note: str | None
    # Set when the status changed: what caused it, for the event journal.
    cause: str | None = None


def decide(state: StreamState, obs: Observation, policy: HealthPolicy) -> Decision:
    s = state
    if obs.vantage == "device" and not obs.uncounted:
        field = "last_device_ok_at" if obs.success else "last_device_fail_at"
        s = replace(s, **{field: obs.at})

    note = obs.uncounted or _device_overrides(s, obs, policy)
    if note:
        return Decision(s, counted=False, note=note)

    if obs.reason:
        return _finish(state, replace(s, availability_reason=obs.reason), obs, obs.reason)

    if obs.success:
        reason = None if s.availability_reason in PROBE_REASONS else s.availability_reason
        s = replace(s, status="active", availability_reason=reason, last_ok_at=obs.at,
                    first_fail_at=None, streak_failures=0, streak_windows=0,
                    last_fail_window=None)  # fmt: skip
        return _finish(state, s, obs, "playback_ok" if obs.vantage == "device" else "probe_ok")

    window = int(obs.at.timestamp() // (policy.window_minutes * 60))
    s = replace(
        s,
        first_fail_at=(s.first_fail_at if s.streak_failures else None) or obs.at,
        streak_failures=s.streak_failures + 1,
        streak_windows=s.streak_windows + (window != s.last_fail_window),
        last_fail_window=window,
    )
    cause = "failures"
    if s.status == "unverified":
        s = replace(s, status="new")
    if (
        s.status in ("new", "active")
        and s.streak_failures >= policy.suspect_failures
        and s.streak_windows >= policy.suspect_windows
    ):
        s = replace(s, status="suspect")
    if s.status == "suspect" and obs.at - s.first_fail_at >= timedelta(
        days=policy.quarantine_after_days
    ):
        s, cause = replace(s, status="quarantine"), "failing_for_days"
    aged = age(s, obs.at, policy)
    if aged.status != s.status:
        cause = "quarantine_expired"
    return _finish(state, aged, obs, cause)


def age(state: StreamState, now: datetime, policy: HealthPolicy) -> StreamState:
    """Transitions that need only time: a long quarantine becomes archived."""
    since = state.status_changed_at
    if (
        state.status == "quarantine"
        and since is not None
        and now - since >= timedelta(days=policy.archive_after_days)
    ):
        return replace(state, status="archived", status_changed_at=now)
    return state


def next_check(
    state: StreamState,
    now: datetime,
    policy: HealthPolicy,
    *,
    counted: bool,
    first_seen: datetime | None = None,
) -> datetime:
    if not counted:
        # The result told us nothing about the stream; try again after a window.
        return now + timedelta(minutes=policy.window_minutes)
    if state.status in ("unverified", "new", "active") and state.streak_failures:
        # Spread retries over separate windows so a flap is told apart from an outage.
        return now + timedelta(minutes=policy.window_minutes)
    planned = now + timedelta(minutes=RECHECK_MINUTES[state.status])
    if in_trial(first_seen, now, policy):
        return min(planned, now + timedelta(minutes=TRIAL_MINUTES))
    return planned


def in_trial(first_seen: datetime | None, now: datetime, policy: HealthPolicy) -> bool:
    return first_seen is not None and now - first_seen < timedelta(hours=policy.trial_hours)


def _device_overrides(s: StreamState, obs: Observation, policy: HealthPolicy) -> str | None:
    """A probe that disagrees with a recent device result is recorded but not counted."""
    if obs.vantage == "device":
        return None
    trust = timedelta(hours=policy.device_trust_hours)
    ok, fail = s.last_device_ok_at, s.last_device_fail_at
    device_ok = ok is not None and obs.at - ok <= trust and (fail is None or fail < ok)
    device_fail = fail is not None and obs.at - fail <= trust and (ok is None or ok < fail)
    if not obs.success and device_ok:
        return "device_recent_ok"
    if obs.success and device_fail:
        return "device_recent_fail"
    return None


def _finish(before: StreamState, after: StreamState, obs: Observation, cause: str) -> Decision:
    if after.status != before.status:
        after = replace(after, status_changed_at=obs.at)
        return Decision(after, counted=True, note=None, cause=cause)
    return Decision(after, counted=True, note=None)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
