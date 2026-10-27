"""Deterministic sample data for the demo dashboard: three services whose
delivery performance differs, including a bad week for one of them."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from opsctl.dora.metrics import Commit, Deployment, Incident

PROFILES = {
    # repo: (deploys per weekday, failure probability, commit-to-deploy hours)
    "acme/checkout-api": (3.0, 0.04, (1, 10)),
    "acme/payments": (1.0, 0.09, (4, 40)),
    "acme/reporting": (0.25, 0.18, (24, 160)),
}


def generate(end: datetime | None = None, days: int = 180, seed: int = 7):
    rng = random.Random(seed)
    end = end or datetime(2026, 10, 27, tzinfo=UTC)
    start = end - timedelta(days=days)
    deploys, commits, incidents = [], [], []
    for repo, (rate, fail_p, (lt_lo, lt_hi)) in PROFILES.items():
        day = start
        while day < end:
            if day.weekday() < 5:
                for _ in range(_poisson(rng, rate)):
                    shipped = day + timedelta(hours=rng.uniform(9, 19))
                    # a rough patch for payments eight weeks ago
                    bad_week = repo == "acme/payments" and 49 <= (end - shipped).days <= 56
                    ok = rng.random() > (0.45 if bad_week else fail_p)
                    lead = timedelta(hours=rng.uniform(lt_lo, lt_hi))
                    for k in range(rng.randint(1, 4)):
                        commits.append(Commit(repo, f"{repo}-{shipped:%j%H%M}-{k}",
                                              shipped - lead - timedelta(minutes=17 * k)))
                    deploys.append(Deployment(repo, f"{shipped:%j%H%M}", shipped - lead, shipped, ok))
                    if not ok:
                        fix = shipped + timedelta(hours=rng.uniform(0.3, 6 if rate >= 1 else 30))
                        deploys.append(Deployment(repo, f"{fix:%j%H%M}f", shipped - lead, fix, True))
                    elif rng.random() < fail_p / 3:
                        opened = shipped + timedelta(hours=rng.uniform(0.5, 12))
                        incidents.append(Incident(repo, opened,
                                                  opened + timedelta(hours=rng.uniform(0.5, 8))))
            day += timedelta(days=1)
    return deploys, commits, incidents, start, end


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, threshold = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rng.random()
        if p <= threshold:
            return n
        n += 1
