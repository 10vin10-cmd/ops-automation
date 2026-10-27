"""CDK stack: run runbooks when CloudWatch alarms fire.

    EventBridge (alarm → ALARM) → Lambda (runbook engine) → ECS / CloudWatch
                                        ↘ DynamoDB (locks, run counters, executions)
                                        ↘ SNS (notifications, approval requests)

Safe by default: the function deploys in dry-run mode and only touches ECS
services in the clusters you name.

    cdk deploy -c clusters=prod -c dryRun=false -c notifyEmail=oncall@example.com
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "runbook-lambda"


def package_function() -> Path:
    """The engine has no third-party dependencies at runtime (boto3 ships with
    Lambda); runbooks are converted from YAML to JSON so PyYAML isn't needed."""
    if BUILD.exists():
        shutil.rmtree(BUILD)
    shutil.copytree(ROOT / "opsctl", BUILD / "opsctl",
                    ignore=shutil.ignore_patterns("__pycache__", "demo.py", "cli.py"))
    (BUILD / "runbooks").mkdir(parents=True)
    for f in sorted((ROOT / "runbooks").glob("*.y*ml")):
        (BUILD / "runbooks" / f"{f.stem}.json").write_text(json.dumps(yaml.safe_load(f.read_text())))
    return BUILD


class RunbooksStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        clusters = [c for c in str(self.node.try_get_context("clusters") or "").split(",") if c]
        dry_run = str(self.node.try_get_context("dryRun") or "true").lower() != "false"
        email = self.node.try_get_context("notifyEmail")

        table = ddb.Table(
            self, "State",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,  # the audit trail outlives the stack
        )

        topic = sns.Topic(self, "Notifications", display_name="Runbook notifications",
                          master_key=kms.Alias.from_alias_name(self, "SnsKey", "alias/aws/sns"))
        if email:
            topic.add_subscription(subs.EmailSubscription(email))

        dlq = sqs.Queue(self, "FailedEvents", retention_period=Duration.days(14), enforce_ssl=True)
        fn = lambda_.Function(
            self, "Engine",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="opsctl.runbooks.lambda_handler.handler",
            code=lambda_.Code.from_asset(str(package_function())),
            timeout=Duration.seconds(60),
            memory_size=256,
            # Bounded blast radius: an alarm storm can't fan out into dozens of
            # simultaneous remediations (the per-target lock is the main guard).
            reserved_concurrent_executions=5,
            retry_attempts=1,
            dead_letter_queue=dlq,
            environment={"TABLE_NAME": table.table_name, "TOPIC_ARN": topic.topic_arn,
                         "RUNBOOK_DIR": "runbooks", "DRY_RUN": "true" if dry_run else "false"},
            log_group=logs.LogGroup(self, "EngineLogs", retention=logs.RetentionDays.THREE_MONTHS),
        )
        table.grant_read_write_data(fn)
        topic.grant_publish(fn)

        service_arns = ([f"arn:{Aws.PARTITION}:ecs:{Aws.REGION}:{Aws.ACCOUNT_ID}:service/{c}/*"
                         for c in clusters] or ["*"])
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ecs:DescribeServices", "ecs:UpdateService"], resources=service_arns))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ecs:ListTaskDefinitions", "ecs:DescribeTaskDefinition",
                     "cloudwatch:GetMetricStatistics"], resources=["*"]))

        events.Rule(
            self, "AlarmFired",
            description="CloudWatch alarms entering ALARM trigger runbooks",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"], detail_type=["CloudWatch Alarm State Change"],
                detail={"state": {"value": ["ALARM"]}}),
            targets=[targets.LambdaFunction(fn, dead_letter_queue=dlq, retry_attempts=2)],
        )

        # Watch the watcher: tell people if the engine itself is failing.
        cw.Alarm(self, "EngineErrors", metric=fn.metric_errors(period=Duration.minutes(5)),
                 threshold=1, evaluation_periods=1,
                 alarm_description="Runbook engine errors",
                 treat_missing_data=cw.TreatMissingData.NOT_BREACHING
                 ).add_alarm_action(cw_actions.SnsAction(topic))

        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "TopicArn", value=topic.topic_arn)
        CfnOutput(self, "DryRun", value=str(dry_run))
