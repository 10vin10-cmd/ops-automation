import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from opsctl.runbooks import actions, engine, store
from opsctl.runbooks.engine import Status
from opsctl.runbooks.expr import ExpressionError, evaluate
from opsctl.runbooks.lambda_handler import alarm_from_event

ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS = engine.load_runbooks(ROOT / "runbooks")
DIMS = {"ClusterName": "prod", "ServiceName": "payments"}

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


class FullStrength:
    """moto never starts tasks; report services as fully running."""

    def __init__(self, ecs, running=None):
        self._ecs, self._running = ecs, running

    def __getattr__(self, name):
        return getattr(self._ecs, name)

    def describe_services(self, **kw):
        resp = self._ecs.describe_services(**kw)
        for s in resp["services"]:
            s["runningCount"] = s["desiredCount"] if self._running is None else self._running
        return resp


@pytest.fixture
def ecs():
    with mock_aws():
        client = boto3.client("ecs")
        client.create_cluster(clusterName="prod")
        for _ in range(2):
            client.register_task_definition(family="payments", containerDefinitions=[
                {"name": "app", "image": "x", "memory": 256}])
        client.create_service(cluster="prod", serviceName="payments", taskDefinition="payments:2",
                              desiredCount=2)
        yield client


def run(ecs, minutes_after_deploy, mem=None, running=None, **ctx):
    mem = mem or store.MemoryStore()
    c = actions.Context(ecs=FullStrength(ecs, running),
                        now=datetime.now(UTC) + timedelta(minutes=minutes_after_deploy), **ctx)
    eng = engine.Engine(mem, c)
    return eng, eng.handle_alarm(RUNBOOKS, "payments-5xx", "ALARM", DIMS)


def desired(ecs):
    return ecs.describe_services(cluster="prod", services=["payments"])["services"][0]


# -- expressions -------------------------------------------------------------------

def test_expressions():
    names = {"service": {"running": 1, "desired": 3}, "rollback": {"ran": False}}
    assert evaluate("service.running < service.desired and not rollback.ran", names)
    assert not evaluate("service.missing < 5", names)      # unknown values never compare true
    assert evaluate("service.desired * 2 == 6", names)


@pytest.mark.parametrize("bad", ["__import__('os').system('x')", "service['running']",
                                 "[x for x in y]", "lambda: 1"])
def test_expressions_cannot_execute_code(bad):
    with pytest.raises(ExpressionError):
        evaluate(bad, {})


def test_runbook_validation():
    with pytest.raises(ValueError, match="unknown action"):
        engine.Runbook.from_dict({"id": "x", "trigger": {}, "steps": [{"id": "a", "action": "rm_rf"}]})
    with pytest.raises(ValueError, match="exactly one"):
        engine.Runbook.from_dict({"id": "x", "trigger": {}, "steps": [{"id": "a"}]})


# -- engine paths --------------------------------------------------------------------

def test_recent_deploy_is_rolled_back(ecs):
    _, [ex] = run(ecs, minutes_after_deploy=5)
    assert ex.status == Status.SUCCEEDED
    assert ex.outputs["rollback"]["rolled_back_to"] == "payments:1"
    assert ex.outputs["restart"] == {"ran": False}
    assert desired(ecs)["taskDefinition"].endswith("payments:1")


def test_missing_tasks_are_replaced_when_no_recent_deploy(ecs):
    _, [ex] = run(ecs, minutes_after_deploy=300, running=1)
    assert ex.outputs["rollback"] == {"ran": False}
    assert ex.outputs["restart"]["restarted"] == "payments"


def test_scale_out_waits_for_approval_then_rechecks(ecs):
    mem = store.MemoryStore()
    eng, [ex] = run(ecs, minutes_after_deploy=300, mem=mem)
    assert ex.status == Status.WAITING_APPROVAL and ex.waiting_step == "scale_out"
    assert desired(ecs)["desiredCount"] == 2  # nothing changed yet

    rb = RUNBOOKS[0]
    done = eng.decide(rb, ex.id, approver="vinod", approve=True)
    assert done.status == Status.SUCCEEDED
    assert desired(ecs)["desiredCount"] == 4
    events = [(a["actor"], a["event"], a["step"]) for a in done.audit]
    assert ("vinod", "approved", "scale_out") in events
    assert ("vinod", "action", "scale_out") in events
    assert events.count(("system", "check", "service")) == 2  # checks ran again after approval


def test_approved_step_is_skipped_if_its_condition_no_longer_holds(ecs):
    mem = store.MemoryStore()
    eng, [ex] = run(ecs, minutes_after_deploy=300, mem=mem)
    # While waiting, tasks went missing: the restart path now applies instead.
    eng.ctx.ecs = FullStrength(ecs, running=0)
    done = eng.decide(RUNBOOKS[0], ex.id, approver="vinod", approve=True)
    assert done.outputs["restart"]["ran"] is True
    assert done.outputs["scale_out"] == {"ran": False}
    assert any(a["event"] == "skipped" and a["step"] == "scale_out" for a in done.audit)


def test_rejection_stops_the_execution(ecs):
    eng, [ex] = run(ecs, minutes_after_deploy=300)
    done = eng.decide(RUNBOOKS[0], ex.id, approver="lead", approve=False)
    assert done.status == Status.REJECTED and desired(ecs)["desiredCount"] == 2
    with pytest.raises(ValueError, match="not waiting"):
        eng.decide(RUNBOOKS[0], ex.id, approver="lead", approve=True)


def test_scaling_respects_the_ceiling(ecs):
    ecs.update_service(cluster="prod", service="payments", desiredCount=6)
    eng, [ex] = run(ecs, minutes_after_deploy=300)
    done = eng.decide(RUNBOOKS[0], ex.id, approver="vinod", approve=True)
    assert done.outputs["scale_out"]["scaled_to"] == 8  # max: 8, not 12


def test_cooldown_and_daily_limit(ecs):
    mem = store.MemoryStore()
    _, [first] = run(ecs, 5, mem=mem)
    _, [second] = run(ecs, 10, mem=mem)
    assert first.status == Status.SUCCEEDED and second.status == Status.SKIPPED
    assert "cooldown" in second.audit[-1]["detail"]

    for k in range(2):  # after each cooldown expires
        run(ecs, 5 + 31 * (k + 1), mem=mem)
    _, [fourth] = run(ecs, 5 + 31 * 3, mem=mem)
    assert fourth.status == Status.SKIPPED
    assert any("daily limit" in str(a["detail"]) for a in fourth.audit)


def test_dry_run_changes_nothing(ecs):
    _, [ex] = run(ecs, 5, dry_run=True)
    assert ex.outputs["rollback"]["would"].startswith("roll payments back")
    assert desired(ecs)["taskDefinition"].endswith("payments:2")


def test_failures_are_recorded_and_announced(ecs):
    ctx = actions.Context(ecs=FullStrength(ecs), now=datetime.now(UTC) + timedelta(minutes=5))
    rb = engine.Runbook.from_dict({"id": "broken", "trigger": {"alarm": "*"}, "target": {},
                                   "steps": [{"id": "svc", "check": "ecs_service",
                                              "params": {"cluster": "prod", "service": "nope"}}]})
    ex = engine.Engine(store.MemoryStore(), ctx).start(rb, "x", {})
    assert ex.status == Status.FAILED
    assert ctx.notifications and "failed at svc" in ctx.notifications[0]


def test_unrelated_alarms_trigger_nothing(ecs):
    eng = engine.Engine(store.MemoryStore(), actions.Context(ecs=ecs))
    assert eng.handle_alarm(RUNBOOKS, "payments-latency", "ALARM", DIMS) == []
    assert eng.handle_alarm(RUNBOOKS, "payments-5xx", "OK", DIMS) == []


# -- stores --------------------------------------------------------------------------

def test_dynamo_store_guards_hold_across_writers():
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        table = ddb.create_table(TableName="t", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                                 AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                                 BillingMode="PAY_PER_REQUEST")
        a, b = store.DynamoStore(table), store.DynamoStore(table)  # two concurrent invocations
        now = datetime.now(UTC)
        assert a.acquire("k", now, timedelta(minutes=30))
        assert not b.acquire("k", now + timedelta(minutes=1), timedelta(minutes=30))
        assert b.acquire("k", now + timedelta(minutes=31), timedelta(minutes=30))
        assert [a.count_run("k", "2026-10-27", 2) for _ in range(3)] == [True, True, False]
        a.save({"id": "e1", "runbook": "r", "target": {}, "status": "succeeded", "started_at": "x"})
        assert b.load("e1").runbook == "r"


def test_file_store_survives_between_commands(tmp_path, ecs):
    path = tmp_path / "state.json"
    eng, [ex] = run(ecs, 300, mem=store.FileStore(path))
    assert ex.status == Status.WAITING_APPROVAL
    later = engine.Engine(store.FileStore(path), actions.Context(ecs=FullStrength(ecs)))
    assert later.decide(RUNBOOKS[0], ex.id, "vinod", True).status == Status.SUCCEEDED


# -- Lambda --------------------------------------------------------------------------

EVENT = {"detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch",
         "detail": {"alarmName": "payments-5xx", "state": {"value": "ALARM"},
                    "configuration": {"metrics": [{"id": "m1", "metricStat": {"metric": {
                        "namespace": "AWS/ApplicationELB", "name": "HTTPCode_Target_5XX_Count",
                        "dimensions": {"ClusterName": "prod", "ServiceName": "payments"}}}}]}}}


def test_alarm_event_parsing():
    assert alarm_from_event(EVENT) == ("payments-5xx", "ALARM", DIMS)


def test_packaged_lambda_runs_without_third_party_dependencies(tmp_path):
    """Build the function exactly as the stack does, then run it in a fresh
    interpreter where PyYAML can't be imported."""
    sys.path.insert(0, str(ROOT))
    from infra.runbooks_stack import package_function
    build = package_function()
    script = tmp_path / "invoke.py"
    script.write_text(f"""
import sys, json, os
import boto3
from moto import mock_aws       # moto itself uses PyYAML, so load it first...
sys.modules['yaml'] = None      # ...then make PyYAML unimportable, as in Lambda
sys.path.insert(0, {str(build)!r})
os.chdir({str(build)!r})
with mock_aws():
    boto3.resource('dynamodb').create_table(
        TableName='t', KeySchema=[{{'AttributeName': 'pk', 'KeyType': 'HASH'}}],
        AttributeDefinitions=[{{'AttributeName': 'pk', 'AttributeType': 'S'}}],
        BillingMode='PAY_PER_REQUEST')
    ecs = boto3.client('ecs'); ecs.create_cluster(clusterName='prod')
    ecs.register_task_definition(
        family='payments', containerDefinitions=[{{'name': 'a', 'image': 'x', 'memory': 256}}])
    ecs.create_service(cluster='prod', serviceName='payments', taskDefinition='payments:1', desiredCount=1)
    from opsctl.runbooks.lambda_handler import handler
    print(json.dumps(handler(json.loads({json.dumps(json.dumps(EVENT))}), None)))
""")
    env = {**os.environ, "TABLE_NAME": "t", "RUNBOOK_DIR": "runbooks", "DRY_RUN": "true"}
    proc = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["executions"][0]["runbook"] == "ecs-service-5xx"
    assert result["executions"][0]["status"] == "succeeded"
