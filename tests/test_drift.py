import json
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from opsctl.drift import cloudformation, policy, remediate, report, terraform
from opsctl.drift.model import Change, Drift, diff
from opsctl.drift.remediate import Mode

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "drift" / "refresh-only-plan.json"
POLICY = policy.Policy.load()


def classify_all(drifts):
    return [POLICY.classify(d) for d in drifts]


def by_address(plan):
    return {i.address: i for i in plan.items}


# -- diff -------------------------------------------------------------------------

def test_diff_reports_leaf_changes():
    assert diff({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}}) == [Change("a.c", 2, 3)]


def test_diff_reports_added_and_removed_list_items():
    out = diff({"rules": [{"port": 443}]}, {"rules": [{"port": 443}, {"port": 22}]})
    assert out == [Change("rules[+]", None, {"port": 22})]
    out = diff({"arns": ["x", "y"]}, {"arns": ["x"]})
    assert out == [Change("arns[-]", "y", None)]


# -- terraform ----------------------------------------------------------------------

def test_terraform_plan_parsing():
    drifts = {d.address: d for d in terraform.load(EXAMPLE)}
    assert len(drifts) == 7
    assert drifts["aws_cloudwatch_metric_alarm.payments_5xx"].status == "deleted"
    assert drifts["aws_ecs_service.payments"].changes == [Change("desired_count", 2, 6)]
    assert drifts["aws_instance.reporting"].only_tags_changed


def test_volatile_attributes_are_ignored():
    plan = {"resource_drift": [{"address": "aws_x.a", "type": "aws_x", "change": {
        "actions": ["update"], "before": {"id": "1", "etag": "a", "name": "n"},
        "after": {"id": "1", "etag": "b", "name": "n"}}}]}
    assert terraform.parse_plan(plan) == []


# -- policy ---------------------------------------------------------------------------

def test_every_example_drift_gets_the_intended_rule():
    rules = {c.drift.address: c.rule.id for c in classify_all(terraform.load(EXAMPLE))}
    assert rules == {
        "aws_security_group.orders_api": "public-ingress",
        "aws_s3_bucket_public_access_block.invoices": "public-bucket",
        "aws_ecs_service.payments": "capacity-change",
        "aws_instance.reporting": "tags-only",
        "aws_iam_role.payments_task": "iam-change",
        "aws_cloudwatch_metric_alarm.payments_5xx": "resource-deleted",
        "aws_security_group.lab_scratch": "public-ingress",
    }


def test_private_ingress_change_is_not_public_ingress():
    d = Drift("terraform", "aws_security_group.x", "aws_security_group", "modified",
              [Change("ingress[0].cidr_blocks[0]", "10.0.0.0/16", "10.1.0.0/16")])
    assert POLICY.classify(d).rule.id == "other-drift"


def test_most_severe_rule_wins():
    d = Drift("terraform", "aws_security_group.x", "aws_security_group", "modified",
              [Change("tags.env", "a", "b"), Change("ingress[+]", None, {"cidr_blocks": ["0.0.0.0/0"]})])
    assert POLICY.classify(d).rule.id == "public-ingress"


def test_cloudformation_resource_types_match_too():
    d = Drift("cloudformation", "stack/Bucket", "AWS::S3::Bucket", "modified",
              [Change("PublicAccessBlockConfiguration.BlockPublicPolicy", True, False)])
    assert POLICY.classify(d).rule.id == "public-bucket"


# -- remediation plan and guardrails ---------------------------------------------------

def test_plan_modes_for_the_example():
    plan = remediate.plan(classify_all(terraform.load(EXAMPLE)), POLICY)
    m = {a: i.mode for a, i in by_address(plan).items()}
    assert m["aws_security_group.orders_api"] == Mode.AUTO
    assert m["aws_s3_bucket_public_access_block.invoices"] == Mode.AUTO
    assert m["aws_security_group.lab_scratch"] == Mode.SKIPPED        # tagged ops:drift=ignore
    assert m["aws_iam_role.payments_task"] == Mode.NEEDS_APPROVAL
    assert m["aws_cloudwatch_metric_alarm.payments_5xx"] == Mode.NEEDS_APPROVAL
    assert m["aws_ecs_service.payments"] == Mode.REPORT
    assert m["aws_instance.reporting"] == Mode.REPORT


def test_approvals_by_address_or_rule():
    drifts = classify_all(terraform.load(EXAMPLE))
    plan = remediate.plan(drifts, POLICY, {"aws_iam_role.payments_task", "resource-deleted"})
    m = {a: i.mode for a, i in by_address(plan).items()}
    assert m["aws_iam_role.payments_task"] == Mode.APPROVED
    assert m["aws_cloudwatch_metric_alarm.payments_5xx"] == Mode.APPROVED


def test_auto_revert_limit_per_run():
    open_sg = [Drift("terraform", f"aws_security_group.s{i}", "aws_security_group", "modified",
                     [Change("ingress[+]", None, {"cidr_blocks": ["0.0.0.0/0"]})]) for i in range(7)]
    plan = remediate.plan(classify_all(open_sg), POLICY)
    modes = [i.mode for i in plan.items]
    assert modes.count(Mode.AUTO) == POLICY.max_auto_reverts_per_run == 5
    assert modes.count(Mode.NEEDS_APPROVAL) == 2


def test_cloudformation_revert_is_manual():
    d = Drift("cloudformation", "stack/Sg", "AWS::EC2::SecurityGroup", "modified",
              [Change("SecurityGroupIngress", [], [{"CidrIp": "0.0.0.0/0"}])])
    plan = remediate.plan(classify_all([d]), POLICY)
    assert plan.items[0].mode == Mode.MANUAL


def test_execute_is_a_dry_run_unless_applied():
    plan = remediate.plan(classify_all(terraform.load(EXAMPLE)), POLICY)
    calls = []

    def runner(cmd, cwd):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0, "Apply complete!", "")

    dry = remediate.execute(plan, "infra", apply=False, runner=runner)
    assert not dry.applied and calls == []
    assert "-target=aws_security_group.orders_api" in dry.command
    assert "-target=aws_security_group.lab_scratch" not in dry.command

    real = remediate.execute(plan, "infra", apply=True, runner=runner)
    assert real.applied and real.ok and calls[0][1] == "infra"


def test_failed_apply_is_reported():
    plan = remediate.plan(classify_all(terraform.load(EXAMPLE)), POLICY)
    result = remediate.execute(plan, ".", apply=True,
                               runner=lambda c, d: subprocess.CompletedProcess(c, 1, "", "Error: x"))
    assert result.applied and not result.ok


def test_report_lists_approvals_and_codify_items():
    plan = remediate.plan(classify_all(terraform.load(EXAMPLE)), POLICY)
    md = report.markdown(plan, remediate.execute(plan, ".", apply=False))
    assert "🔴 3 critical" in md
    assert "`ingress[+]` added" in md
    assert "### Needs approval" in md and "`aws_iam_role.payments_task`" in md
    assert "### Codify or undo" in md and "`desired_count` = 6" in md
    assert "Remediation (dry run)" in md


def test_no_drift_report():
    assert "No drift" in report.markdown(remediate.Plan())


# -- CloudFormation detection -------------------------------------------------------

def test_cloudformation_detection_polls_then_reads_drifts():
    cfn = boto3.client("cloudformation", region_name="ap-south-1", aws_access_key_id="x",
                       aws_secret_access_key="x")
    with Stubber(cfn) as stub:
        stub.add_response("detect_stack_drift", {"StackDriftDetectionId": "d-1"}, {"StackName": "web"})
        common = {"StackId": "arn:aws:cloudformation:ap-south-1:1:stack/web/x",
                  "StackDriftDetectionId": "d-1", "Timestamp": "2026-10-27T00:00:00Z"}
        stub.add_response("describe_stack_drift_detection_status",
                          {**common, "DetectionStatus": "DETECTION_IN_PROGRESS"},
                          {"StackDriftDetectionId": "d-1"})
        stub.add_response("describe_stack_drift_detection_status",
                          {**common, "DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "DRIFTED"},
                          {"StackDriftDetectionId": "d-1"})
        stub.add_response("describe_stack_resource_drifts", {"StackResourceDrifts": [{
            "StackId": common["StackId"], "LogicalResourceId": "WebSg",
            "PhysicalResourceId": "sg-1", "ResourceType": "AWS::EC2::SecurityGroup",
            "StackResourceDriftStatus": "MODIFIED", "Timestamp": "2026-10-27T00:00:00Z",
            "PropertyDifferences": [{"PropertyPath": "/SecurityGroupIngress/1/CidrIp",
                                     "ExpectedValue": "10.0.0.0/16", "ActualValue": "0.0.0.0/0",
                                     "DifferenceType": "NOT_EQUAL"}]}]},
            {"StackName": "web", "StackResourceDriftStatusFilters": ["MODIFIED", "DELETED"]})
        drifts = cloudformation.detect(cfn, "web", sleep=lambda s: None)
    assert drifts[0].address == "web/WebSg"
    assert drifts[0].changes == [Change("SecurityGroupIngress.1.CidrIp", "10.0.0.0/16", "0.0.0.0/0")]
    assert POLICY.classify(drifts[0]).rule.id == "public-ingress"


def test_cloudformation_detection_failure_raises():
    cfn = boto3.client("cloudformation", region_name="ap-south-1", aws_access_key_id="x",
                       aws_secret_access_key="x")
    with Stubber(cfn) as stub:
        stub.add_response("detect_stack_drift", {"StackDriftDetectionId": "d-1"}, {"StackName": "web"})
        stub.add_response("describe_stack_drift_detection_status", {
            "StackId": "s", "StackDriftDetectionId": "d-1", "Timestamp": "2026-10-27T00:00:00Z",
            "DetectionStatus": "DETECTION_FAILED", "DetectionStatusReason": "no permission"},
            {"StackDriftDetectionId": "d-1"})
        with pytest.raises(RuntimeError, match="no permission"):
            cloudformation.detect(cfn, "web", sleep=lambda s: None)


def test_cli_exit_code_signals_work_for_people(tmp_path):
    from opsctl.cli import main
    out = tmp_path / "drift.md"
    assert main(["drift", "terraform", str(EXAMPLE), "--out", str(out)]) == 1  # approvals pending
    assert "Needs approval" in out.read_text()
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"resource_drift": []}))
    assert main(["drift", "terraform", str(clean)]) == 0
