import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infra.runbooks_stack import RunbooksStack


def template(**context) -> Template:
    return Template.from_stack(RunbooksStack(cdk.App(context=context), "Test"))


def test_dry_run_by_default():
    template().has_resource_properties("AWS::Lambda::Function", {
        "Environment": {"Variables": Match.object_like({"DRY_RUN": "true"})}})


def test_live_mode_is_opt_in():
    template(dryRun="false").has_resource_properties("AWS::Lambda::Function", {
        "Environment": {"Variables": Match.object_like({"DRY_RUN": "false"})}})


def test_concurrency_is_bounded():
    template().has_resource_properties("AWS::Lambda::Function",
                                       {"ReservedConcurrentExecutions": 5})


def test_ecs_permissions_are_scoped_to_named_clusters():
    policies = template(clusters="prod,staging").find_resources("AWS::IAM::Policy")
    statements = [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]
    update = next(s for s in statements if "ecs:UpdateService" in s["Action"])
    assert len(update["Resource"]) == 2
    assert all(":service/" in str(r) for r in update["Resource"])


def test_triggered_by_alarms_entering_alarm_state():
    template().has_resource_properties("AWS::Events::Rule", {"EventPattern": {
        "source": ["aws.cloudwatch"], "detail-type": ["CloudWatch Alarm State Change"],
        "detail": {"state": {"value": ["ALARM"]}}}})


def test_audit_table_is_retained_and_expires_locks():
    t = template()
    t.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True}})


def test_engine_failures_alert_people():
    template().has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "Errors", "AlarmActions": Match.any_value()})
