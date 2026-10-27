"""The four DORA metrics, computed from deployments, commits and incidents.

Definitions used here (each is a choice; they're documented on the dashboard):

  Deployment frequency   successful production deployments per day
  Lead time for changes  median time from a commit landing on main to the first
                         successful deployment that includes it
  Change failure rate    failed deployments, plus successful ones followed by an
                         incident within 24 hours, over all deployments
  Recovery time          median time to restore: failed deployment → next
                         successful one, and incident opened → resolved

Performance bands default to DORA's 2023 clusters (elite, high, medium, low)
and can be overridden.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Deployment:
    repo: str
    sha: str
    commit_time: datetime       # when the deployed head commit was made
    finished: datetime
    succeeded: bool


@dataclass(frozen=True)
class Commit:
    repo: str
    sha: str
    time: datetime


@dataclass(frozen=True)
class Incident:
    repo: str
    opened: datetime
    resolved: datetime | None


@dataclass
class Bands:
    """Thresholds for elite / high / medium; anything worse is low."""
    deploys_per_day: tuple[float, float, float] = (1.0, 1 / 7, 1 / 30)
    lead_time_h: tuple[float, float, float] = (24, 24 * 7, 24 * 30)
    change_failure_rate: tuple[float, float, float] = (0.05, 0.10, 0.15)
    recovery_h: tuple[float, float, float] = (1, 24, 24 * 7)


LEVELS = ("elite", "high", "medium", "low")


def _band_higher_is_better(value: float, t: tuple[float, float, float]) -> str:
    return next((lvl for lvl, th in zip(LEVELS, t, strict=False) if value >= th), "low")


def _band_lower_is_better(value: float, t: tuple[float, float, float]) -> str:
    return next((lvl for lvl, th in zip(LEVELS, t, strict=False) if value <= th), "low")


@dataclass
class Metric:
    name: str
    value: float | None
    unit: str
    band: str | None
    samples: int
    previous: float | None = None
    weekly: list[float | None] = field(default_factory=list)


@dataclass
class Scorecard:
    start: datetime
    end: datetime
    repos: list[str]
    metrics: dict[str, Metric]
    per_repo: dict[str, dict[str, Metric]] = field(default_factory=dict)


def _median_h(deltas: list[timedelta]) -> float | None:
    return statistics.median(d.total_seconds() / 3600 for d in deltas) if deltas else None


def lead_times(deploys: list[Deployment], commits: list[Commit]) -> list[tuple[Commit, timedelta]]:
    ok = sorted((d for d in deploys if d.succeeded), key=lambda d: d.finished)
    out = []
    for c in commits:
        shipped = next((d for d in ok if d.repo == c.repo and d.commit_time >= c.time
                        and d.finished >= c.time), None)
        if shipped:
            out.append((c, shipped.finished - c.time))
    return out


def recoveries(deploys: list[Deployment], incidents: list[Incident]) -> list[timedelta]:
    out = []
    by_repo: dict[str, list[Deployment]] = {}
    for d in sorted(deploys, key=lambda d: d.finished):
        by_repo.setdefault(d.repo, []).append(d)
    for ds in by_repo.values():
        for i, d in enumerate(ds):
            if not d.succeeded and (i == 0 or ds[i - 1].succeeded):  # start of a failure streak
                fixed = next((n for n in ds[i + 1:] if n.succeeded), None)
                if fixed:
                    out.append(fixed.finished - d.finished)
    out += [i.resolved - i.opened for i in incidents if i.resolved]
    return out


def failures(deploys: list[Deployment], incidents: list[Incident]) -> int:
    """Failed deployments, plus successful ones blamed for an incident. An
    incident is blamed on the latest successful deployment of its repo in the
    24 hours before it, and on no other."""
    failed = sum(1 for d in deploys if not d.succeeded)
    blamed: set[Deployment] = set()
    ok = sorted((d for d in deploys if d.succeeded), key=lambda d: d.finished)
    for i in incidents:
        before = [d for d in ok if d.repo == i.repo
                  and timedelta(0) <= i.opened - d.finished <= timedelta(hours=24)]
        if before:
            blamed.add(before[-1])
    return failed + len(blamed)


def compute(deploys, commits, incidents, start: datetime, end: datetime,
            bands: Bands | None = None):
    bands = bands or Bands()
    def window(items, key, a, b):
        return [x for x in items if a <= key(x) < b]

    def four(a: datetime, b: datetime) -> dict[str, tuple[float | None, int]]:
        ds = window(deploys, lambda d: d.finished, a, b)
        cs = window(commits, lambda c: c.time, a, b)
        inc = window(incidents, lambda i: i.opened, a, b)
        days = max((b - a).total_seconds() / 86400, 1e-9)
        ok = [d for d in ds if d.succeeded]
        lts = [t for _, t in lead_times(deploys, cs)]
        recs = recoveries(ds, inc)
        return {
            "deployment_frequency": (len(ok) / days, len(ok)),
            "lead_time": (_median_h(lts), len(lts)),
            "change_failure_rate": (failures(ds, inc) / len(ds) if ds else None, len(ds)),
            "recovery_time": (_median_h(recs), len(recs)),
        }

    span = end - start
    now, before = four(start, end), four(start - span, start)
    weeks = []
    w = start
    while w < end:
        weeks.append(four(w, min(w + timedelta(days=7), end)))
        w += timedelta(days=7)

    meta = {
        "deployment_frequency": ("Deployment frequency", "per day",
                                 lambda v: _band_higher_is_better(v, bands.deploys_per_day)),
        "lead_time": ("Lead time for changes", "hours",
                      lambda v: _band_lower_is_better(v, bands.lead_time_h)),
        "change_failure_rate": ("Change failure rate", "ratio",
                                lambda v: _band_lower_is_better(v, bands.change_failure_rate)),
        "recovery_time": ("Recovery time", "hours",
                          lambda v: _band_lower_is_better(v, bands.recovery_h)),
    }
    metrics = {}
    for key, (label, unit, band) in meta.items():
        value, n = now[key]
        metrics[key] = Metric(label, value, unit, band(value) if value is not None else None, n,
                              previous=before[key][0], weekly=[wk[key][0] for wk in weeks])
    return metrics


def scorecard(deploys, commits, incidents, start, end, bands: Bands | None = None) -> Scorecard:
    bands = bands or Bands()
    repos = sorted({d.repo for d in deploys} | {c.repo for c in commits})
    card = Scorecard(start, end, repos, compute(deploys, commits, incidents, start, end, bands))
    for repo in repos:
        card.per_repo[repo] = compute([d for d in deploys if d.repo == repo],
                                      [c for c in commits if c.repo == repo],
                                      [i for i in incidents if i.repo == repo], start, end, bands)
    return card
