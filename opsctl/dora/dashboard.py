"""Render a scorecard as a single self-contained HTML page (no scripts, no
external assets) and as Markdown for a CI job summary."""

from __future__ import annotations

import html

from opsctl.dora.metrics import Bands, Metric, Scorecard

HIGHER_IS_BETTER = {"deployment_frequency"}
BAND_COLOURS = {"elite": "var(--elite)", "high": "var(--high)", "medium": "var(--medium)",
                "low": "var(--low)"}


def fmt(key: str, v: float | None) -> str:
    if v is None:
        return "–"
    if key == "deployment_frequency":
        return f"{v:.1f} / day" if v >= 1 else f"{v * 7:.1f} / week" if v * 7 >= 1 \
            else f"{v * 30:.1f} / month"
    if key == "change_failure_rate":
        return f"{v:.0%}"
    if v < 1:
        return f"{v * 60:.0f} min"
    return f"{v:.1f} h" if v < 48 else f"{v / 24:.1f} days"


def _delta(key: str, m: Metric) -> tuple[str, str]:
    if m.value is None or m.previous is None:
        return "", ""
    if key == "change_failure_rate":
        # A rate's change reads best in percentage points.
        pts = (m.value - m.previous) * 100
        if abs(pts) < 0.5:
            return "→ steady vs previous period", "flat"
        return (f"{'▲' if pts > 0 else '▼'} {abs(pts):.0f} pts vs previous period",
                "bad" if pts > 0 else "good")
    if m.previous == 0:
        return "", ""
    change = (m.value - m.previous) / abs(m.previous)
    if abs(change) < 0.05:
        return "→ steady vs previous period", "flat"
    better = (change > 0) == (key in HIGHER_IS_BETTER)
    arrow = "▲" if change > 0 else "▼"
    return f"{arrow} {abs(change):.0%} vs previous period", "good" if better else "bad"


def _spark(values: list[float | None], w: int = 220, h: int = 44) -> str:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true"></svg>'
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    coords = [(4 + i * (w - 8) / n, h - 4 - (v - lo) / span * (h - 8)) for i, v in pts]
    path = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y) in enumerate(coords))
    lx, ly = coords[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="weekly trend">'
            f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="currentColor"/></svg>')


def _unit(key: str) -> str:
    if key in ("deployment_frequency", "change_failure_rate"):
        return "deployments"
    return "changes" if key == "lead_time" else "recoveries"


def _chip(band: str | None) -> str:
    if not band:
        return '<span class="chip none">no data</span>'
    return f'<span class="chip" style="--c:{BAND_COLOURS[band]}">{band}</span>'


def render_html(card: Scorecard, title: str = "Delivery scorecard", sample: bool = False,
                bands: Bands | None = None) -> str:
    bands = bands or Bands()
    e = html.escape
    tiles = []
    for key, m in card.metrics.items():
        delta, tone = _delta(key, m)
        tiles.append(f"""
      <article class="tile">
        <header><h2>{e(m.name)}</h2>{_chip(m.band)}</header>
        <p class="value">{e(fmt(key, m.value))}</p>
        <p class="delta {tone}">{e(delta)}</p>
        <div class="trend">{_spark(m.weekly)}</div>
        <p class="n">{m.samples} {_unit(key)} measured</p>
      </article>""")

    rows = "".join(
        f"<tr><th scope='row'>{e(repo)}</th>" + "".join(
            f"<td>{e(fmt(k, ms[k].value))} {_chip(ms[k].band)}</td>" for k in card.metrics)
        + "</tr>" for repo, ms in card.per_repo.items())
    heads = "".join(f"<th scope='col'>{e(m.name)}</th>" for m in card.metrics.values())

    def th(key, vals):
        op = "≥" if key in HIGHER_IS_BETTER else "≤"
        return "".join(f"<td>{op} {e(fmt(key, v))}</td>" for v in vals)

    banner = ('<p class="banner">Sample data, generated to show the dashboard. '
              'Run <code>opsctl dora</code> against your repositories for real numbers.</p>'
              if sample else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --ink:#18202a; --muted:#5d6874; --line:#dde3e8;
  --elite:#1e7b4a; --high:#2f6fae; --medium:#a86b00; --low:#b3261e; --good:#1e7b4a; --bad:#b3261e;
  color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#12171c; --card:#1a2229; --ink:#e7edf2;
  --muted:#9aa7b2; --line:#2d3944; --elite:#6fd39b; --high:#7db6ea; --medium:#f0c062; --low:#f08a82;
  --good:#6fd39b; --bad:#f08a82; color-scheme: dark; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:2rem 1rem 3rem; }}
h1 {{ margin:0 0 .2rem; font-size:1.6rem; }}
.sub, .n, .delta.flat {{ color:var(--muted); }}
.banner {{ background:var(--card); border:1px dashed var(--line); padding:.6rem .9rem; border-radius:8px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:1rem;
  margin:1.5rem 0; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem 1.1rem; }}
.tile header {{ display:flex; justify-content:space-between; align-items:center; gap:.5rem; }}
.tile h2 {{ font-size:.85rem; font-weight:600; color:var(--muted); margin:0; text-transform:uppercase;
  letter-spacing:.04em; }}
.value {{ font-size:1.9rem; font-weight:650; margin:.4rem 0 0; }}
.delta {{ margin:0; font-size:.88rem; min-height:1.3em; }}
.delta.good {{ color:var(--good); }} .delta.bad {{ color:var(--bad); }}
.trend {{ color:var(--high); margin:.4rem 0 0; }}
.spark {{ width:100%; height:44px; display:block; }}
.n {{ font-size:.8rem; margin:.2rem 0 0; }}
.chip {{ font-size:.75rem; font-weight:650; padding:.12rem .55rem; border-radius:999px; white-space:nowrap;
  color:var(--c); background:color-mix(in srgb, var(--c) 14%, transparent); text-transform:capitalize; }}
.chip.none {{ --c: var(--muted); }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem 1.1rem;
  margin-top:1rem; overflow-x:auto; }}
section h2 {{ font-size:1.05rem; margin:0 0 .6rem; }}
table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
th, td {{ text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--line); white-space:nowrap; }}
thead th {{ color:var(--muted); font-weight:600; font-size:.8rem; }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:.35rem 1rem; margin:0; font-size:.9rem; }}
dt {{ font-weight:600; }} dd {{ margin:0; color:var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>{e(title)}</h1>
  <p class="sub">{card.start:%d %b %Y} – {card.end:%d %b %Y} · {len(card.repos)} repositories ·
    trends are weekly</p>
  {banner}
  <div class="tiles">{"".join(tiles)}
  </div>
  <section aria-labelledby="by-repo"><h2 id="by-repo">By repository</h2>
    <table><thead><tr><th scope="col">Repository</th>{heads}</tr></thead><tbody>{rows}</tbody></table>
  </section>
  <section aria-labelledby="how"><h2 id="how">How these are measured</h2>
    <dl>
      <dt>Deployment frequency</dt><dd>Successful runs of the deploy workflow on the default branch.</dd>
      <dt>Lead time for changes</dt><dd>Median time from a commit landing on the default branch to the
        first successful deployment that includes it.</dd>
      <dt>Change failure rate</dt><dd>Failed deployments, plus successful ones followed within 24 hours
        by an issue labelled <code>incident</code>, divided by all deployments.</dd>
      <dt>Recovery time</dt><dd>Median time from a failed deployment to the next successful one, and
        from an incident being opened to being closed.</dd>
    </dl>
  </section>
  <section aria-labelledby="bands"><h2 id="bands">Performance bands</h2>
    <p class="n">Defaults follow DORA's 2023 performance clusters; anything worse than medium is low.</p>
    <table><thead><tr><th></th><th>Elite</th><th>High</th><th>Medium</th></tr></thead><tbody>
      <tr><th scope="row">Deployment frequency</th>{th("deployment_frequency", bands.deploys_per_day)}</tr>
      <tr><th scope="row">Lead time</th>{th("lead_time", bands.lead_time_h)}</tr>
      <tr><th scope="row">Change failure rate</th>{th("change_failure_rate", bands.change_failure_rate)}</tr>
      <tr><th scope="row">Recovery time</th>{th("recovery_time", bands.recovery_h)}</tr>
    </tbody></table>
  </section>
</main>
</body>
</html>
"""


def render_markdown(card: Scorecard) -> str:
    lines = [f"## Delivery scorecard ({card.start:%d %b} – {card.end:%d %b %Y})", "",
             "| Metric | Value | Band | vs previous |", "|---|---|---|---|"]
    for key, m in card.metrics.items():
        lines.append(f"| {m.name} | {fmt(key, m.value)} | {m.band or '–'} | {_delta(key, m)[0] or '–'} |")
    lines += ["", "| Repository | " + " | ".join(m.name for m in card.metrics.values()) + " |",
              "|---" * (len(card.metrics) + 1) + "|"]
    for repo, ms in card.per_repo.items():
        lines.append(f"| {repo} | " + " | ".join(
            f"{fmt(k, ms[k].value)} ({ms[k].band or '–'})" for k in card.metrics) + " |")
    return "\n".join(lines)
