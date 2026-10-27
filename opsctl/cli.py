"""`opsctl`: drift, DORA scorecard, runbooks and cost cleanup.

    opsctl drift terraform drift.json [--approve RULE_OR_ADDRESS] [--apply --workdir infra/]
    opsctl drift cloudformation my-stack
    opsctl dora --repo owner/name [--repo ...] [--days 90] --html scorecard.html
    opsctl runbook list
    opsctl runbook run --alarm payments-5xx --dimension ClusterName=prod --dimension ServiceName=payments
    opsctl runbook approve <execution-id> | reject <execution-id> | show <execution-id>
    opsctl cost scan [--apply]
    opsctl demo                      everything, against a mocked AWS account
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _emit(markdown: str, out: str | None) -> None:
    print(markdown)
    if out:
        Path(out).write_text(markdown + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def cmd_drift(args) -> int:
    from opsctl.drift import cloudformation, policy, remediate, report, terraform
    pol = policy.Policy.load(args.policy) if args.policy else policy.Policy.load()
    if args.source == "terraform":
        drifts = terraform.load(args.target)
    else:
        import boto3
        drifts = cloudformation.detect(boto3.client("cloudformation"), args.target)
    plan = remediate.plan([pol.classify(d) for d in drifts], pol, set(args.approve))
    result = remediate.execute(plan, args.workdir, apply=args.apply) \
        if args.source == "terraform" else None
    _emit(report.markdown(plan, result), args.out)
    if result and result.applied and not result.ok:
        return 2
    # Non-zero when drift needs a person, so a scheduled job shows up red.
    return 1 if any(i.mode.value in ("needs_approval", "manual") for i in plan.items) else 0


def cmd_dora(args) -> int:
    from opsctl.dora import dashboard, github, metrics, sample
    end = datetime.now(UTC)
    if args.sample:
        d, c, i, _, end = sample.generate()
    else:
        d, c, i = [], [], []
        since = end - timedelta(days=args.days * 2)  # previous period too, for trends
        for repo in args.repo:
            rd, rc, ri = github.collect(repo, since, deploy_workflow=args.workflow,
                                        incident_label=args.incident_label)
            d, c, i = d + rd, c + rc, i + ri
    card = metrics.scorecard(d, c, i, end - timedelta(days=args.days), end)
    _emit(dashboard.render_markdown(card), args.out)
    if args.html:
        Path(args.html).write_text(dashboard.render_html(card, title=args.title, sample=args.sample))
        print(f"\nDashboard written to {args.html}")
    return 0


def _engine(args, dry_run=False):
    from opsctl.runbooks import actions, engine, store
    if args.table:
        import boto3
        st = store.DynamoStore(boto3.resource("dynamodb").Table(args.table))
    else:
        st = store.FileStore(args.state)
    ctx_kwargs = {"dry_run": dry_run}
    if not args.offline:
        import boto3
        ctx_kwargs.update(ecs=boto3.client("ecs"), cloudwatch=boto3.client("cloudwatch"),
                          sns=boto3.client("sns"), topic_arn=args.topic)
    return engine.Engine(st, actions.Context(**ctx_kwargs)), engine.load_runbooks(args.runbooks)


def cmd_runbook(args) -> int:
    from opsctl.runbooks import report
    from opsctl.runbooks.engine import Status, load_runbooks
    if args.action == "list":
        for rb in load_runbooks(args.runbooks):
            print(f"{rb.id:24} on {rb.trigger.get('alarm')} ({rb.trigger.get('state', 'ALARM')}): "
                  f"{len(rb.steps)} steps")
        return 0
    eng, runbooks = _engine(args, dry_run=getattr(args, "dry_run", False))
    if args.action == "run":
        dims = dict(d.split("=", 1) for d in args.dimension)
        executions = eng.handle_alarm(runbooks, args.alarm, args.state, dims)
        if not executions:
            print(f"No runbook matches alarm {args.alarm} ({args.state}).")
        for ex in executions:
            _emit(report.markdown(ex), args.out)
        return 0 if all(e.status != Status.FAILED for e in executions) else 1
    ex = eng.store.load(args.execution_id)
    if args.action == "show":
        _emit(report.markdown(ex), args.out)
        return 0
    rb = next(r for r in runbooks if r.id == ex.runbook)
    who = args.approver or os.environ.get("USER", "unknown")
    ex = eng.decide(rb, args.execution_id, approver=who, approve=args.action == "approve")
    _emit(report.markdown(ex), args.out)
    return 0 if ex.status != Status.FAILED else 1


def cmd_cost(args) -> int:
    import boto3

    from opsctl.cost import scan as cost
    cfg = cost.Config(unattached_days=args.unattached_days, snapshot_days=args.snapshot_days,
                      required_tags=args.required_tag or ["owner", "cost-centre"],
                      max_actions=args.max_actions)
    region = boto3.session.Session().region_name
    findings = cost.scan(boto3.client("ec2"), boto3.client("elbv2"), cfg, region=region)
    results = cost.cleanup(boto3.client("ec2"), findings, apply=args.apply, cfg=cfg)
    _emit(cost.markdown(findings, results), args.out)
    return 1 if any(r.outcome == "failed" for r in results) else 0


def cmd_demo(args) -> int:
    from opsctl import demo
    outputs = demo.run(Path(args.out_dir))
    for name in ("drift.md", "runbooks.md", "cost.md", "scorecard.md"):
        print(f"\n{'=' * 78}\n{name}\n{'=' * 78}\n{outputs[name]}")
    print(f"\nReports and scorecard.html written to {args.out_dir}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("drift", help="detect and remediate infrastructure drift")
    s.add_argument("source", choices=["terraform", "cloudformation"])
    s.add_argument("target", help="terraform: plan JSON file; cloudformation: stack name")
    s.add_argument("--policy", help="policy file (default policies/drift.yaml)")
    s.add_argument("--approve", action="append", default=[], help="address or rule id")
    s.add_argument("--apply", action="store_true", help="really revert (default: dry run)")
    s.add_argument("--workdir", default=".", help="terraform working directory")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_drift)

    s = sub.add_parser("dora", help="DORA scorecard from GitHub")
    s.add_argument("--repo", action="append", default=[])
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--workflow", default="release.yml", help="the deploy workflow file")
    s.add_argument("--incident-label", default="incident")
    s.add_argument("--html")
    s.add_argument("--title", default="Delivery scorecard")
    s.add_argument("--sample", action="store_true", help="use generated sample data")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_dora)

    s = sub.add_parser("runbook", help="alarm-driven runbooks")
    s.add_argument("action", choices=["list", "run", "approve", "reject", "show"])
    s.add_argument("execution_id", nargs="?")
    s.add_argument("--runbooks", default=str(ROOT / "runbooks"))
    s.add_argument("--alarm")
    s.add_argument("--state", default="ALARM")
    s.add_argument("--dimension", action="append", default=[], help="Name=Value")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--approver")
    s.add_argument("--table", help="DynamoDB table (default: local state file)")
    s.add_argument("--state-file", dest="state", default=".opsctl/state.json")
    s.add_argument("--topic", help="SNS topic ARN for notifications")
    s.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_runbook)

    s = sub.add_parser("cost", help="find and clean up waste")
    s.add_argument("action", choices=["scan"])
    s.add_argument("--apply", action="store_true", help="really clean up (default: dry run)")
    s.add_argument("--unattached-days", type=int, default=7)
    s.add_argument("--snapshot-days", type=int, default=90)
    s.add_argument("--required-tag", action="append")
    s.add_argument("--max-actions", type=int, default=20)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_cost)

    s = sub.add_parser("demo", help="run everything against a mocked AWS account")
    s.add_argument("--out-dir", default="demo-output")
    s.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    if args.command == "runbook":
        if args.action == "run" and not args.alarm:
            p.error("runbook run needs --alarm")
        if args.action in ("approve", "reject", "show") and not args.execution_id:
            p.error(f"runbook {args.action} needs an execution id")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
