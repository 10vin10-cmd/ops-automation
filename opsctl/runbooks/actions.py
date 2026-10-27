"""Checks (read-only) and actions (change something) that runbooks can use.

Every action supports dry run, and the ones that change capacity enforce a
ceiling from the runbook (`max`) so a runbook can never scale without bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class Context:
    ecs: Any = None
    cloudwatch: Any = None
    sns: Any = None
    topic_arn: str | None = None
    dry_run: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    notifications: list[str] = field(default_factory=list)  # also kept for the audit trail


# -- checks ---------------------------------------------------------------------

def ecs_service(ctx: Context, cluster: str, service: str) -> dict:
    svc = ctx.ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    current = svc["taskDefinition"]
    primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
    age = None
    if primary and primary.get("createdAt"):
        created = primary["createdAt"]
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
        age = round((ctx.now - created).total_seconds() / 60, 1)
    return {"cluster": cluster, "service": service, "running": svc.get("runningCount", 0),
            "desired": svc.get("desiredCount", 0), "pending": svc.get("pendingCount", 0),
            "task_definition": current, "deployments": len(svc.get("deployments", [])),
            "last_deploy_age_min": age,
            "previous_task_definition": _previous_revision(ctx, current)}


def _previous_revision(ctx: Context, task_def_arn: str) -> str | None:
    family_rev = task_def_arn.rsplit("/", 1)[-1]
    family, _, rev = family_rev.rpartition(":")
    arns = ctx.ecs.list_task_definitions(familyPrefix=family, sort="DESC")["taskDefinitionArns"]
    older = [a for a in arns if a.rsplit("/", 1)[-1].rpartition(":")[0] == family
             and int(a.rsplit(":", 1)[-1]) < int(rev)]
    return older[0] if older else None


def metric(ctx: Context, namespace: str, name: str, dimensions: dict[str, str],
           minutes: int = 10, stat: str = "Sum") -> dict:
    end = ctx.now
    start = end - timedelta(minutes=minutes)
    resp = ctx.cloudwatch.get_metric_statistics(
        Namespace=namespace, MetricName=name, StartTime=start, EndTime=end, Period=60,
        Statistics=[stat], Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()])
    points = [p[stat] for p in resp.get("Datapoints", [])]
    return {"value": sum(points) if stat == "Sum" else (max(points) if points else 0),
            "datapoints": len(points)}


CHECKS = {"ecs_service": ecs_service, "metric": metric}


# -- actions --------------------------------------------------------------------

def ecs_rollback(ctx: Context, cluster: str, service: str, to: str) -> dict:
    if not to:
        raise ValueError("no previous task definition to roll back to")
    if ctx.dry_run:
        return {"would": f"roll {service} back to {to.rsplit('/', 1)[-1]}"}
    ctx.ecs.update_service(cluster=cluster, service=service, taskDefinition=to)
    return {"rolled_back_to": to.rsplit("/", 1)[-1]}


def ecs_restart(ctx: Context, cluster: str, service: str) -> dict:
    if ctx.dry_run:
        return {"would": f"replace all tasks of {service}"}
    ctx.ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
    return {"restarted": service}


def ecs_scale(ctx: Context, cluster: str, service: str, current: int, factor: float = 2,
              max: int = 10) -> dict:  # noqa: A002 - mirrors the runbook key
    desired = min(max, math.ceil(current * factor))
    if desired <= current:
        return {"skipped": f"already at {current}, ceiling is {max}"}
    if ctx.dry_run:
        return {"would": f"scale {service} from {current} to {desired} (ceiling {max})"}
    ctx.ecs.update_service(cluster=cluster, service=service, desiredCount=desired)
    return {"scaled_from": current, "scaled_to": desired}


def notify(ctx: Context, message: str, subject: str = "Runbook") -> dict:
    ctx.notifications.append(message)
    if ctx.sns and ctx.topic_arn and not ctx.dry_run:
        ctx.sns.publish(TopicArn=ctx.topic_arn, Subject=subject[:100], Message=message)
        return {"sent": True}
    return {"sent": False, "message": message}


ACTIONS = {"ecs_rollback": ecs_rollback, "ecs_restart": ecs_restart, "ecs_scale": ecs_scale,
           "notify": notify}
