from datetime import UTC, datetime, timedelta

import pytest

from opsctl.dora import dashboard, github, metrics, sample
from opsctl.dora.metrics import Bands, Commit, Deployment, Incident

T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def dep(at, ok=True, commit_at=None, repo="r"):
    return Deployment(repo, f"sha{at:%H%M}", commit_at or at, at, ok)


def test_lead_time_is_commit_to_first_deploy_that_includes_it():
    commits = [Commit("r", "a", h(0)), Commit("r", "b", h(5))]
    deploys = [dep(h(2), commit_at=h(0)), dep(h(9), commit_at=h(5))]
    lts = {c.sha: t for c, t in metrics.lead_times(deploys, commits)}
    assert lts == {"a": timedelta(hours=2), "b": timedelta(hours=4)}


def test_failed_deploys_do_not_ship_commits():
    commits = [Commit("r", "a", h(0))]
    deploys = [dep(h(1), ok=False, commit_at=h(0)), dep(h(3), commit_at=h(0))]
    assert metrics.lead_times(deploys, commits)[0][1] == timedelta(hours=3)


def test_recovery_from_failed_deploy_and_incident():
    deploys = [dep(h(0)), dep(h(1), ok=False), dep(h(1.5), ok=False), dep(h(3))]
    incidents = [Incident("r", h(10), h(12)), Incident("r", h(20), None)]
    recs = sorted(metrics.recoveries(deploys, incidents))
    assert recs == [timedelta(hours=2), timedelta(hours=2)]  # failure streak counted once


def test_change_failures_include_deploys_followed_by_incidents():
    deploys = [dep(h(0)), dep(h(5), ok=False), dep(h(10))]
    incidents = [Incident("r", h(12), h(13))]  # 2 h after the third deploy
    assert metrics.failures(deploys, incidents) == 2


@pytest.mark.parametrize("per_day,band", [(2, "elite"), (1, "elite"), (0.5, "high"),
                                          (1 / 7, "high"), (0.05, "medium"), (0.01, "low")])
def test_deployment_frequency_bands(per_day, band):
    assert metrics._band_higher_is_better(per_day, Bands().deploys_per_day) == band


@pytest.mark.parametrize("rate,band", [(0.03, "elite"), (0.05, "elite"), (0.08, "high"),
                                       (0.15, "medium"), (0.4, "low")])
def test_change_failure_bands(rate, band):
    assert metrics._band_lower_is_better(rate, Bands().change_failure_rate) == band


def test_scorecard_from_sample_data():
    d, c, i, _, end = sample.generate()
    card = metrics.scorecard(d, c, i, end - timedelta(days=90), end)
    assert set(card.per_repo) == set(sample.PROFILES)
    m = card.metrics
    assert m["deployment_frequency"].band == "elite"
    assert m["lead_time"].value > 0 and m["recovery_time"].samples > 0
    assert len(m["deployment_frequency"].weekly) == 13
    # the team that ships least also fails most
    assert card.per_repo["acme/reporting"]["change_failure_rate"].band == "low"


def test_no_data_is_reported_as_no_data():
    card = metrics.scorecard([], [Commit("r", "a", h(0))], [], T0, h(24 * 7))
    assert card.metrics["change_failure_rate"].value is None
    html = dashboard.render_html(card)
    assert "no data" in html


def test_formatting():
    assert dashboard.fmt("deployment_frequency", 3.14) == "3.1 / day"
    assert dashboard.fmt("deployment_frequency", 2 / 7) == "2.0 / week"
    assert dashboard.fmt("lead_time", 0.5) == "30 min"
    assert dashboard.fmt("lead_time", 72) == "3.0 days"
    assert dashboard.fmt("change_failure_rate", 0.123) == "12%"


def test_failure_rate_change_is_in_points():
    m = metrics.Metric("Change failure rate", 0.12, "ratio", "medium", 10, previous=0.08)
    assert dashboard._delta("change_failure_rate", m) == ("▲ 4 pts vs previous period", "bad")


def test_html_is_self_contained():
    d, c, i, _, end = sample.generate()
    page = dashboard.render_html(metrics.scorecard(d, c, i, end - timedelta(days=90), end), sample=True)
    assert "<script" not in page and "http://" not in page and "https://" not in page
    assert "Sample data" in page and "prefers-color-scheme: dark" in page


def test_github_collector_maps_api_responses():
    repo = "o/r"
    responses = {
        f"{github.API}/repos/{repo}": {"default_branch": "main"},
    }

    def fetch(url):
        if url in responses:
            return responses[url]
        if "/actions/workflows/release.yml/runs" in url:
            return {"workflow_runs": [
                {"status": "completed", "conclusion": "success", "head_sha": "a1",
                 "updated_at": "2026-10-02T10:00:00Z", "created_at": "2026-10-02T09:50:00Z",
                 "head_commit": {"timestamp": "2026-10-02T09:00:00Z"}},
                {"status": "completed", "conclusion": "cancelled", "head_sha": "a2",
                 "updated_at": "2026-10-02T11:00:00Z", "created_at": "2026-10-02T11:00:00Z"},
                {"status": "in_progress", "conclusion": None, "head_sha": "a3",
                 "updated_at": "2026-10-02T12:00:00Z", "created_at": "2026-10-02T12:00:00Z"},
            ]}
        if "/commits?" in url:
            return [{"sha": "a1", "commit": {"committer": {"date": "2026-10-02T09:00:00Z"}}}]
        if "/issues?" in url:
            return [{"created_at": "2026-10-03T00:00:00Z", "closed_at": "2026-10-03T02:00:00Z"},
                    {"created_at": "2026-10-03T00:00:00Z", "closed_at": None, "pull_request": {}}]
        raise AssertionError(url)

    deploys, commits, incidents = github.collect(repo, T0, fetch=fetch)
    assert [(d.sha, d.succeeded) for d in deploys] == [("a1", True)]  # cancelled/running ignored
    assert len(commits) == 1
    assert len(incidents) == 1  # pull requests are not incidents
    assert metrics.lead_times(deploys, commits)[0][1] == timedelta(hours=1)
