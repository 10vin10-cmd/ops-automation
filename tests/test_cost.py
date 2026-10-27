import os
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from opsctl.cost import scan as cost

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
LATER = datetime.now(UTC) + timedelta(days=120)


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.client("ec2"), boto3.client("elbv2")


def kinds(findings, mine=None):
    return sorted((f.kind, f.resource_id) for f in findings
                  if f.kind != "old-snapshot" or (mine and f.resource_id in mine))


def test_finds_unattached_volume_and_idle_ip(aws):
    ec2, _ = aws
    vol = ec2.create_volume(Size=100, AvailabilityZone="ap-south-1a", VolumeType="gp3")
    eip = ec2.allocate_address(Domain="vpc")
    found = {f.resource_id: f for f in cost.scan(ec2, now=LATER)}
    assert found[vol["VolumeId"]].kind == "unattached-volume"
    assert found[vol["VolumeId"]].monthly_usd == pytest.approx(100 * 0.0912)
    assert found[eip["AllocationId"]].kind == "idle-elastic-ip"


def test_recent_volumes_are_not_flagged(aws):
    ec2, _ = aws
    ec2.create_volume(Size=10, AvailabilityZone="ap-south-1a")
    assert not [f for f in cost.scan(ec2) if f.kind == "unattached-volume"]


def test_snapshot_used_by_own_ami_is_kept(aws):
    ec2, _ = aws
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]
    mine = ec2.create_image(InstanceId=inst["InstanceId"], Name="golden")["ImageId"]
    used = {bdm["Ebs"]["SnapshotId"] for bdm in
            ec2.describe_images(ImageIds=[mine])["Images"][0]["BlockDeviceMappings"] if "Ebs" in bdm}
    flagged = {f.resource_id for f in cost.scan(ec2, now=LATER) if f.kind == "old-snapshot"}
    assert used and not (used & flagged)


def test_stopped_instances_and_missing_tags_are_reported_not_cleaned(aws):
    ec2, _ = aws
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[inst])
    findings = [f for f in cost.scan(ec2, now=LATER) if f.resource_id == inst]
    assert {f.kind for f in findings} == {"stopped-instance", "missing-tags"}
    assert all(f.action == "report" for f in findings)
    cost.cleanup(ec2, findings, apply=True)
    assert ec2.describe_instances(InstanceIds=[inst])["Reservations"][0]["Instances"][0][
        "State"]["Name"] == "stopped"  # never terminated


def test_idle_load_balancer(aws):
    ec2, elb = aws
    subnets = [s["SubnetId"] for s in ec2.describe_subnets()["Subnets"][:2]]
    elb.create_load_balancer(Name="old-admin", Subnets=subnets)
    assert any(f.kind == "idle-load-balancer" and f.resource_id == "old-admin"
               for f in cost.scan(ec2, elb, now=LATER))


def test_dry_run_changes_nothing(aws):
    ec2, _ = aws
    vol = ec2.create_volume(Size=10, AvailabilityZone="ap-south-1a")["VolumeId"]
    findings = [f for f in cost.scan(ec2, now=LATER) if f.resource_id == vol]
    results = cost.cleanup(ec2, findings, apply=False)
    assert results[0].outcome == "would" and results[0].note == "snapshot, then delete"
    assert ec2.describe_volumes(VolumeIds=[vol])["Volumes"]


def test_cleanup_snapshots_before_deleting_and_respects_keep(aws):
    ec2, _ = aws
    gone = ec2.create_volume(Size=10, AvailabilityZone="ap-south-1a")["VolumeId"]
    kept = ec2.create_volume(Size=10, AvailabilityZone="ap-south-1a", TagSpecifications=[
        {"ResourceType": "volume", "Tags": [{"Key": "ops:keep", "Value": "true"}]}])["VolumeId"]
    findings = [f for f in cost.scan(ec2, now=LATER) if f.resource_id in (gone, kept)]
    results = {r.finding.resource_id: r for r in cost.cleanup(ec2, findings, apply=True)}
    assert results[gone].outcome == "done" and "backup snap-" in results[gone].note
    assert results[kept].outcome == "skipped"
    left = {v["VolumeId"] for v in ec2.describe_volumes()["Volumes"]}
    assert gone not in left and kept in left
    backups = [s for s in ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
               if {"Key": "opsctl:deleted-volume", "Value": gone} in s.get("Tags", [])]
    assert len(backups) == 1


def test_action_limit_per_run(aws):
    ec2, _ = aws
    for _ in range(4):
        ec2.allocate_address(Domain="vpc")
    findings = [f for f in cost.scan(ec2) if f.kind == "idle-elastic-ip"]
    results = cost.cleanup(ec2, findings, apply=True, cfg=cost.Config(max_actions=3))
    assert [r.outcome for r in results].count("done") == 3
    assert [r.outcome for r in results].count("skipped") == 1


def test_one_failure_does_not_stop_the_run(aws):
    ec2, _ = aws
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ghost = cost.Finding("old-snapshot", "snap-00000000000000000", "x", 1.0, "delete")
    real = next(f for f in cost.scan(ec2) if f.resource_id == eip)
    results = cost.cleanup(ec2, [ghost, real], apply=True)
    assert results[0].outcome == "failed" and results[1].outcome == "done"


def test_markdown_totals():
    md = cost.markdown([cost.Finding("idle-elastic-ip", "eip-1", "x", 3.65, "release"),
                        cost.Finding("unattached-volume", "vol-1", "y", 10.0, "delete")])
    assert "2 finding(s), ~$13.65/month" in md
    assert "Nothing to clean up" in cost.markdown([])
