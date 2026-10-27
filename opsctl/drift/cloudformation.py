"""Drift from CloudFormation's own drift detection.

Detection is asynchronous: start it, poll until it finishes, then read the
resources that are MODIFIED or DELETED with their property differences.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from opsctl.drift.model import Change, Drift


def detect(cfn, stack: str, sleep: Callable[[float], None] = time.sleep,
           timeout_s: float = 300) -> list[Drift]:
    detection_id = cfn.detect_stack_drift(StackName=stack)["StackDriftDetectionId"]
    waited = 0.0
    while True:
        status = cfn.describe_stack_drift_detection_status(StackDriftDetectionId=detection_id)
        if status["DetectionStatus"] != "DETECTION_IN_PROGRESS":
            break
        if waited >= timeout_s:
            raise TimeoutError(f"drift detection for {stack} did not finish in {timeout_s}s")
        sleep(5)
        waited += 5
    if status["DetectionStatus"] == "DETECTION_FAILED":
        raise RuntimeError(f"drift detection failed: {status.get('DetectionStatusReason')}")
    return resource_drifts(cfn, stack)


def resource_drifts(cfn, stack: str) -> list[Drift]:
    out = []
    kwargs = {"StackName": stack, "StackResourceDriftStatusFilters": ["MODIFIED", "DELETED"]}
    while True:
        page = cfn.describe_stack_resource_drifts(**kwargs)
        for r in page["StackResourceDrifts"]:
            changes = [Change(d["PropertyPath"].strip("/").replace("/", "."),
                              _value(d.get("ExpectedValue")), _value(d.get("ActualValue")))
                       for d in r.get("PropertyDifferences", [])]
            out.append(Drift(
                source="cloudformation", address=f"{stack}/{r['LogicalResourceId']}",
                resource_type=r["ResourceType"],
                status="deleted" if r["StackResourceDriftStatus"] == "DELETED" else "modified",
                changes=changes, physical_id=r.get("PhysicalResourceId", ""),
            ))
        if not page.get("NextToken"):
            return out
        kwargs["NextToken"] = page["NextToken"]


def _value(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
