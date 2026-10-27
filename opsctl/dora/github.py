"""Collect deployments, commits and incidents from GitHub.

  deployments  runs of the deploy workflow on the default branch (success or
               failure; cancelled and skipped runs are ignored)
  commits      commits on the default branch
  incidents    issues labelled `incident` (configurable)

Uses the REST API with a token if one is set (GITHUB_TOKEN), which raises the
rate limit and is required for private repositories.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from opsctl.dora.metrics import Commit, Deployment, Incident

API = "https://api.github.com"
Fetch = Callable[[str], object]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def http_fetch(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token := os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _pages(fetch: Fetch, url: str, key: str | None = None, max_pages: int = 10):
    sep = "&" if "?" in url else "?"
    for page in range(1, max_pages + 1):
        data = fetch(f"{url}{sep}per_page=100&page={page}")
        items = data[key] if key else data
        yield from items
        if len(items) < 100:
            return


def collect(repo: str, since: datetime, deploy_workflow: str = "release.yml",
            incident_label: str = "incident", fetch: Fetch = http_fetch):
    meta = fetch(f"{API}/repos/{repo}")
    branch = meta["default_branch"]
    q = urllib.parse.urlencode({"branch": branch, "created": f">={since.date().isoformat()}"})
    deploys = []
    try:
        runs = _pages(fetch, f"{API}/repos/{repo}/actions/workflows/{deploy_workflow}/runs?{q}",
                      "workflow_runs")
        for r in runs:
            if r.get("status") != "completed" or r.get("conclusion") not in ("success", "failure"):
                continue
            head = r.get("head_commit") or {}
            deploys.append(Deployment(
                repo=repo, sha=r["head_sha"],
                commit_time=_parse(head.get("timestamp") or r["created_at"]),
                finished=_parse(r["updated_at"]), succeeded=r["conclusion"] == "success"))
    except urllib.error.HTTPError as err:
        if err.code != 404:  # no such workflow: the repo doesn't deploy
            raise

    commits = [Commit(repo, c["sha"], _parse(c["commit"]["committer"]["date"]))
               for c in _pages(fetch, f"{API}/repos/{repo}/commits?"
                               + urllib.parse.urlencode({"sha": branch, "since": since.isoformat()}))]

    incidents = [Incident(repo, _parse(i["created_at"]),
                          _parse(i["closed_at"]) if i.get("closed_at") else None)
                 for i in _pages(fetch, f"{API}/repos/{repo}/issues?" + urllib.parse.urlencode(
                     {"labels": incident_label, "state": "all", "since": since.isoformat()}))
                 if "pull_request" not in i]
    return deploys, commits, incidents
