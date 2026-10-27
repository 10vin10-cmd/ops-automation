"""`opsctl demo`: every capability end to end, against a mocked AWS account
(moto) and sample data, with no credentials needed. Writes the reports that
docs/demo/ shows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _FullStrength:
    """moto never starts ECS tasks (runningCount stays 0). Report services at
    full strength so the demo can show the paths for a healthy-looking fleet."""

    def __init__(self, ecs):
        self._ecs = ecs

    def __getattr__(self, name):
        return getattr(self._ecs, name)

    def describe_services(self, **kwargs):
        resp = self._ecs.describe_services(**kwargs)
        for svc in resp["services"]:
            svc["runningCount"] = svc["desiredCount"]
        return resp


def run(out_dir: Path) -> dict[str, str]:
    import boto3
    from moto import mock_aws

    from opsctl.cost import scan as cost
    from opsctl.dora import dashboard, metrics, sample
    from opsctl.drift import policy, remediate, report, terraform
    from opsctl.runbooks import actions, engine, report as rb_report, store

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}

    # 1. Drift: a refresh-only Terraform plan with seven kinds of drift.
    pol = policy.Policy.load()
    drifts = terraform.load(ROOT / "examples" / "drift" / "refresh-only-plan.json")
    plan = remediate.plan([pol.classify(d) for d in drifts], pol)
    outputs["drift.md"] = report.markdown(plan, remediate.execute(plan, ".", apply=False))

    # 2. DORA scorecard from sample delivery data.
    d, c, i, _, end = sample.generate()
    card = metrics.scorecard(d, c, i, end - timedelta(days=90), end)
    outputs["scorecard.md"] = dashboard.render_markdown(card)
    (out_dir / "scorecard.html").write_text(dashboard.render_html(card, sample=True))

    with mock_aws():
        region = "ap-south-1"
        ecs = boto3.client("ecs", region_name=region)
        ec2 = boto3.client("ec2", region_name=region)
        elbv2 = boto3.client("elbv2", region_name=region)

        # 3. Runbooks: three alarms on an ECS cluster.
        ecs.create_cluster(clusterName="prod")
        for family, image in (("payments", "payments:41"), ("payments", "payments:42"),
                              ("orders", "orders:17")):
            ecs.register_task_definition(family=family, containerDefinitions=[
                {"name": "app", "image": f"registry.example.com/{image}", "memory": 512}])
        ecs.create_service(cluster="prod", serviceName="payments", taskDefinition="payments:2",
                           desiredCount=2)
        ecs.create_service(cluster="prod", serviceName="orders", taskDefinition="orders:1",
                           desiredCount=3)
        runbooks = engine.load_runbooks(ROOT / "runbooks")
        mem = store.MemoryStore()

        def at(minutes_after_deploy: float) -> actions.Context:
            return actions.Context(ecs=_FullStrength(ecs),
                                   now=datetime.now(UTC) + timedelta(minutes=minutes_after_deploy))

        parts = []
        # a) payments starts failing right after a deploy: automatic rollback
        e1 = engine.Engine(mem, at(5)).handle_alarm(
            runbooks, "payments-5xx", "ALARM", {"ClusterName": "prod", "ServiceName": "payments"})[0]
        parts.append(("Errors right after a deploy: automatic rollback", e1))
        # b) the alarm flaps: cooldown stops a second run
        e2 = engine.Engine(mem, at(8)).handle_alarm(
            runbooks, "payments-5xx", "ALARM", {"ClusterName": "prod", "ServiceName": "payments"})[0]
        parts.append(("The alarm fires again three minutes later: cooldown", e2))
        # c) orders fails long after its deploy: scale-out needs a person
        eng = engine.Engine(mem, at(600))
        e3 = eng.handle_alarm(runbooks, "orders-5xx", "ALARM",
                              {"ClusterName": "prod", "ServiceName": "orders"})[0]
        parts.append(("Errors with no recent deploy: scale-out waits for approval", e3))
        e3b = eng.decide(next(r for r in runbooks if r.id == e3.runbook), e3.id,
                         approver="vinod", approve=True)
        parts.append(("…approved by a person: checks re-run, then it scales", e3b))
        outputs["runbooks.md"] = "\n\n".join(f"#### {title}\n\n{rb_report.markdown(ex)}"
                                             for title, ex in parts)

        # 4. Cost and hygiene: a cluttered account.
        vol = ec2.create_volume(Size=500, AvailabilityZone=f"{region}a", VolumeType="gp3")
        ec2.create_volume(Size=100, AvailabilityZone=f"{region}a", VolumeType="gp2")
        ec2.create_volume(Size=50, AvailabilityZone=f"{region}a", VolumeType="gp3",
                          TagSpecifications=[{"ResourceType": "volume", "Tags": [
                              {"Key": "ops:keep", "Value": "true"}, {"Key": "owner", "Value": "dba"}]}])
        mine = ec2.create_snapshot(VolumeId=vol["VolumeId"], Description="nightly backup, old schema")
        ec2.allocate_address(Domain="vpc")
        ec2.allocate_address(Domain="vpc")
        ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, InstanceType="m5.large",
                                 TagSpecifications=[{"ResourceType": "instance", "Tags": [
                                     {"Key": "owner", "Value": "team-data"}]}])["Instances"][0]
        ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
        subnets = [s["SubnetId"] for s in ec2.describe_subnets()["Subnets"][:2]]
        elbv2.create_load_balancer(Name="legacy-admin", Subnets=subnets)
        later = datetime.now(UTC) + timedelta(days=120)
        # moto ships hundreds of public-AMI snapshots in every account; show ours only
        findings = [f for f in cost.scan(ec2, elbv2, now=later, region=region)
                    if f.kind != "old-snapshot" or f.resource_id == mine["SnapshotId"]]
        outputs["cost.md"] = cost.markdown(findings, cost.cleanup(ec2, findings, apply=False))

    for name, text in outputs.items():
        (out_dir / name).write_text(text + "\n")
    return outputs
