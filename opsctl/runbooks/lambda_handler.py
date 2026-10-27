"""AWS Lambda entry point: EventBridge "CloudWatch Alarm State Change" events.

Environment:
  TABLE_NAME     DynamoDB table for locks, counters and executions
  TOPIC_ARN      SNS topic for notifications and approval requests
  RUNBOOK_DIR    directory of runbook files shipped with the function
  DRY_RUN        "true" to only log what would be done
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from opsctl.runbooks.actions import Context
from opsctl.runbooks.engine import Engine, load_runbooks
from opsctl.runbooks.store import DynamoStore

log = logging.getLogger()
log.setLevel(logging.INFO)


def alarm_from_event(event: dict) -> tuple[str, str, dict[str, str]]:
    detail = event["detail"]
    dims: dict[str, str] = {}
    for m in detail.get("configuration", {}).get("metrics", []):
        dims.update((m.get("metricStat", {}).get("metric", {}) or {}).get("dimensions", {}) or {})
    return detail["alarmName"], detail["state"]["value"], dims


def handler(event, _context):
    alarm, state, dims = alarm_from_event(event)
    engine = Engine(
        DynamoStore(boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])),
        Context(ecs=boto3.client("ecs"), cloudwatch=boto3.client("cloudwatch"),
                sns=boto3.client("sns"), topic_arn=os.environ.get("TOPIC_ARN"),
                dry_run=os.environ.get("DRY_RUN", "").lower() == "true"),
    )
    runbooks = load_runbooks(os.environ.get("RUNBOOK_DIR", "runbooks"))
    executions = engine.handle_alarm(runbooks, alarm, state, dims)
    summary = [{"id": e.id, "runbook": e.runbook, "status": e.status.value} for e in executions]
    log.info(json.dumps({"alarm": alarm, "state": state, "executions": summary}))
    return {"executions": summary}
