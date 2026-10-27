"""Where executions, cooldown locks and run counters live.

The DynamoDB store makes the guardrails hold across concurrent Lambda
invocations: the cooldown lock and the daily counter are single conditional
writes, so two alarms arriving together can't both act.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from botocore.exceptions import ClientError


class Store(Protocol):
    def acquire(self, key: str, now: datetime, cooldown: timedelta) -> bool: ...
    def count_run(self, key: str, day: str, limit: int) -> bool: ...
    def save(self, execution: dict) -> None: ...
    def load(self, execution_id: str): ...


class MemoryStore:
    def __init__(self):
        self.locks: dict[str, datetime] = {}
        self.counts: dict[str, int] = {}
        self.executions: dict[str, dict] = {}

    def acquire(self, key, now, cooldown):
        until = self.locks.get(key)
        if until and until > now:
            return False
        self.locks[key] = now + cooldown
        return True

    def count_run(self, key, day, limit):
        k = f"{key}#{day}"
        if self.counts.get(k, 0) >= limit:
            return False
        self.counts[k] = self.counts.get(k, 0) + 1
        return True

    def save(self, execution):
        self.executions[execution["id"]] = execution

    def load(self, execution_id):
        from opsctl.runbooks.engine import Execution
        return Execution.from_dict(self.executions[execution_id])


class DynamoStore:
    """Single table, partition key `pk` (string)."""

    def __init__(self, table):
        self.table = table

    def acquire(self, key, now, cooldown):
        try:
            self.table.put_item(
                Item={"pk": f"LOCK#{key}", "until": (now + cooldown).isoformat(),
                      "expires_at": int((now + cooldown).timestamp()) + 86400},
                ConditionExpression="attribute_not_exists(pk) OR #u < :now",
                ExpressionAttributeNames={"#u": "until"},
                ExpressionAttributeValues={":now": now.isoformat()})
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def count_run(self, key, day, limit):
        try:
            self.table.update_item(
                Key={"pk": f"RUNS#{key}#{day}"},
                UpdateExpression="ADD runs :one",
                ConditionExpression="attribute_not_exists(runs) OR runs < :limit",
                ExpressionAttributeValues={":one": 1, ":limit": limit})
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def save(self, execution):
        import json
        self.table.put_item(Item={"pk": f"EXEC#{execution['id']}",
                                  "doc": json.dumps(execution, default=str),
                                  "status": execution["status"]})

    def load(self, execution_id):
        import json

        from opsctl.runbooks.engine import Execution
        item = self.table.get_item(Key={"pk": f"EXEC#{execution_id}"}, ConsistentRead=True).get("Item")
        if not item:
            raise KeyError(f"no execution {execution_id}")
        return Execution.from_dict(json.loads(item["doc"]))


class FileStore(MemoryStore):
    """MemoryStore persisted to a JSON file, for running runbooks from the CLI
    (approve/reject happen in a later command)."""

    def __init__(self, path):
        import json
        from pathlib import Path
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.locks = {k: datetime.fromisoformat(v) for k, v in raw.get("locks", {}).items()}
            self.counts = raw.get("counts", {})
            self.executions = raw.get("executions", {})

    def _flush(self):
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "locks": {k: v.isoformat() for k, v in self.locks.items()},
            "counts": self.counts, "executions": self.executions}, indent=2, default=str))

    def acquire(self, key, now, cooldown):
        ok = super().acquire(key, now, cooldown)
        self._flush()
        return ok

    def count_run(self, key, day, limit):
        ok = super().count_run(key, day, limit)
        self._flush()
        return ok

    def save(self, execution):
        super().save(execution)
        self._flush()
