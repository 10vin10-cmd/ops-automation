"""Find waste and hygiene problems in an AWS account, and clean up safely.

Findings
  unattached-volume    EBS volume not attached to anything for N days
  old-snapshot         snapshot older than N days and not used by any AMI
  idle-elastic-ip      Elastic IP not associated with anything
  stopped-instance     instance stopped for more than N days (its disks still bill)
  idle-load-balancer   load balancer with no registered targets
  missing-tags         resource without the required tags (cost allocation)

Cleanup is dry run unless --apply, and:
  - never touches a resource tagged ops:keep=true
  - snapshots a volume before deleting it
  - never terminates instances or deletes load balancers: those are reported
    for a person to decide
  - stops after max_actions per run

Prices are approximate on-demand list prices (Config.prices); check them for
your region. They size the opportunity; they are not a bill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

DEFAULT_PRICES = {               # USD per month, approximate; verify for your region
    "ebs_gb": {"gp3": 0.0912, "gp2": 0.114, "io1": 0.149, "st1": 0.051, "sc1": 0.0174,
               "standard": 0.08},
    "snapshot_gb": 0.05,
    "idle_eip": 3.65,             # $0.005 per hour
    "load_balancer": 18.4,        # ALB base hourly charge, before LCUs
}
KEEP_TAG = "ops:keep"


@dataclass
class Finding:
    kind: str
    resource_id: str
    detail: str
    monthly_usd: float = 0.0
    action: str = "report"        # delete | release | report
    tags: dict = field(default_factory=dict)
    region: str = ""

    @property
    def protected(self) -> bool:
        return self.tags.get(KEEP_TAG, "").lower() == "true"


@dataclass
class Config:
    unattached_days: int = 7
    snapshot_days: int = 90
    stopped_days: int = 14
    required_tags: list[str] = field(default_factory=lambda: ["owner", "cost-centre"])
    prices: dict = field(default_factory=lambda: DEFAULT_PRICES)
    max_actions: int = 20


def _tags(raw) -> dict:
    return {t["Key"]: t["Value"] for t in raw or []}


def _age_days(when: datetime, now: datetime) -> float:
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return (now - when).total_seconds() / 86400


_STOP_REASON = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)")


def scan(ec2, elbv2=None, cfg: Config | None = None, now: datetime | None = None,
         region: str = "") -> list[Finding]:
    cfg = cfg or Config()
    now = now or datetime.now(UTC)
    out: list[Finding] = []
    prices = cfg.prices

    for v in _all(ec2, "describe_volumes", "Volumes",
                  Filters=[{"Name": "status", "Values": ["available"]}]):
        age = _age_days(v["CreateTime"], now)
        if age >= cfg.unattached_days:
            cost = v["Size"] * prices["ebs_gb"].get(v.get("VolumeType", "gp3"), 0.1)
            out.append(Finding("unattached-volume", v["VolumeId"],
                               f"{v['Size']} GiB {v.get('VolumeType', '')}, unattached, "
                               f"created {age:.0f} days ago", cost, "delete", _tags(v.get("Tags")),
                               region))

    in_use = {bdm["Ebs"]["SnapshotId"]
              for img in _all(ec2, "describe_images", "Images", Owners=["self"])
              for bdm in img.get("BlockDeviceMappings", []) if bdm.get("Ebs", {}).get("SnapshotId")}
    old = [s for s in _all(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"])
           if _age_days(s["StartTime"], now) >= cfg.snapshot_days and s["SnapshotId"] not in in_use]
    # Snapshots made for an AMI name it in their description ("... for ami-123").
    # If that AMI still exists anywhere, the snapshot is in use: deleting it fails.
    named = {m.group(0) for s in old if (m := _AMI.search(s.get("Description", "")))}
    live_amis = _existing_images(ec2, sorted(named))
    for s in old:
        m = _AMI.search(s.get("Description", ""))
        if m and m.group(0) in live_amis:
            continue
        age = _age_days(s["StartTime"], now)
        out.append(Finding("old-snapshot", s["SnapshotId"],
                           f"{s['VolumeSize']} GiB, {age:.0f} days old, not used by an AMI",
                           s["VolumeSize"] * prices["snapshot_gb"], "delete",
                           _tags(s.get("Tags")), region))

    for a in ec2.describe_addresses()["Addresses"]:
        if not a.get("AssociationId") and not a.get("InstanceId") and not a.get("NetworkInterfaceId"):
            out.append(Finding("idle-elastic-ip", a["AllocationId"], f"{a['PublicIp']} not associated",
                               prices["idle_eip"], "release", _tags(a.get("Tags")), region))

    for r in _all(ec2, "describe_instances", "Reservations"):
        for i in r["Instances"]:
            tags = _tags(i.get("Tags"))
            missing = [t for t in cfg.required_tags if t not in tags]
            if missing and i["State"]["Name"] != "terminated":
                out.append(Finding("missing-tags", i["InstanceId"],
                                   f"missing {', '.join(missing)}", 0.0, "report", tags, region))
            if i["State"]["Name"] == "stopped":
                m = _STOP_REASON.search(i.get("StateTransitionReason", ""))
                stopped = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) \
                    if m else None
                if stopped and _age_days(stopped, now) >= cfg.stopped_days:
                    out.append(Finding("stopped-instance", i["InstanceId"],
                                       f"{i['InstanceType']} stopped {_age_days(stopped, now):.0f} "
                                       "days ago; its volumes still bill", 0.0, "report", tags, region))

    if elbv2 is not None:
        for lb in elbv2.describe_load_balancers()["LoadBalancers"]:
            try:
                groups = elbv2.describe_target_groups(
                    LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            except ClientError as err:
                if err.response["Error"]["Code"] != "TargetGroupNotFound":
                    raise
                groups = []  # some implementations raise instead of returning []
            targets = sum(len(elbv2.describe_target_health(TargetGroupArn=g["TargetGroupArn"])
                              ["TargetHealthDescriptions"]) for g in groups)
            if targets == 0:
                out.append(Finding("idle-load-balancer", lb["LoadBalancerName"],
                                   f"{lb.get('Type', 'application')} load balancer with no targets",
                                   prices["load_balancer"], "report", {}, region))
    return sorted(out, key=lambda f: -f.monthly_usd)


_AMI = re.compile(r"\bami-[0-9a-f]+\b")


def _existing_images(ec2, image_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(image_ids), 100):
        try:
            found |= {img["ImageId"] for img in
                      ec2.describe_images(ImageIds=image_ids[i:i + 100])["Images"]}
        except ClientError:
            for image_id in image_ids[i:i + 100]:  # one bad id fails the batch
                try:
                    found |= {img["ImageId"] for img in
                              ec2.describe_images(ImageIds=[image_id])["Images"]}
                except ClientError:
                    pass
    return found


def _all(client, method: str, key: str, **kwargs):
    paginator = client.get_paginator(method)
    for page in paginator.paginate(**kwargs):
        yield from page[key]


@dataclass
class CleanupResult:
    finding: Finding
    outcome: str        # done | would | skipped
    note: str = ""


def cleanup(ec2, findings: list[Finding], apply: bool = False,
            cfg: Config | None = None) -> list[CleanupResult]:
    cfg = cfg or Config()
    results: list[CleanupResult] = []
    acted = 0
    for f in findings:
        if f.action == "report":
            continue
        if f.protected:
            results.append(CleanupResult(f, "skipped", f"tagged {KEEP_TAG}=true"))
            continue
        if acted >= cfg.max_actions:
            results.append(CleanupResult(f, "skipped", f"over the limit of {cfg.max_actions} per run"))
            continue
        acted += 1
        if not apply:
            results.append(CleanupResult(f, "would", _describe(f)))
            continue
        try:
            results.append(_clean_one(ec2, f))
        except ClientError as err:  # one failure must not stop the rest
            results.append(CleanupResult(f, "failed", err.response["Error"]["Code"]))
    return results


def _clean_one(ec2, f: Finding) -> CleanupResult:
    if f.kind == "unattached-volume":
        snap = ec2.create_snapshot(VolumeId=f.resource_id,
                                   Description=f"opsctl: backup before deleting {f.resource_id}",
                                   TagSpecifications=[{"ResourceType": "snapshot", "Tags": [
                                       {"Key": "opsctl:deleted-volume", "Value": f.resource_id}]}])
        ec2.delete_volume(VolumeId=f.resource_id)
        return CleanupResult(f, "done", f"deleted; backup {snap['SnapshotId']}")
    if f.kind == "old-snapshot":
        ec2.delete_snapshot(SnapshotId=f.resource_id)
        return CleanupResult(f, "done", "deleted")
    if f.kind == "idle-elastic-ip":
        ec2.release_address(AllocationId=f.resource_id)
        return CleanupResult(f, "done", "released")
    return CleanupResult(f, "skipped", f"no automatic cleanup for {f.kind}")


def _describe(f: Finding) -> str:
    return {"unattached-volume": "snapshot, then delete", "old-snapshot": "delete",
            "idle-elastic-ip": "release"}.get(f.kind, f.action)


def markdown(findings: list[Finding], results: list[CleanupResult] | None = None) -> str:
    total = sum(f.monthly_usd for f in findings)
    out = [f"## Cost and hygiene: {len(findings)} finding(s), ~${total:,.2f}/month", ""]
    if not findings:
        return out[0] + "\n\nNothing to clean up. ✅\n"
    by = {r.finding.resource_id: r for r in results or []}
    out += ["| Finding | Resource | Detail | Est. $/month | Cleanup |", "|---|---|---|---:|---|"]
    for f in findings:
        r = by.get(f.resource_id)
        what = (f"{r.outcome}: {r.note}" if r else
                "report only" if f.action == "report" else "—")
        out.append(f"| {f.kind} | `{f.resource_id}` | {f.detail} | "
                   f"{f.monthly_usd:,.2f} | {what} |")
    out += ["", "Estimates use list prices from the config; they size the opportunity, not the bill."]
    return "\n".join(out)


def since(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)
