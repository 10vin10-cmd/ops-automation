#### Errors right after a deploy: automatic rollback

### ✅ `ecs-service-5xx` on prod / payments: succeeded

Execution `618404a4007a` · alarm `payments-5xx` · started 2026-10-27T18:30:39+00:00

| When | Who | What | Step | Detail |
|---|---|---|---|---|
| 18:30:39 | system | triggered |  | alarm payments-5xx |
| 18:30:39 | system | check | service | {"cluster": "prod", "service": "payments", "running": 2, "desired": 2, "pending": 2, "task_definition": "arn:aws:ecs:ap-south-1:12345678901… |
| 18:30:39 | system | action | rollback | {"rolled_back_to": "payments:1"} |
| 18:30:39 | system | action | report | {"sent": false, "message": "payments on prod: rolled back=True, restarted=False, scaled=False."} |

#### The alarm fires again three minutes later: cooldown

### ⏭️ `ecs-service-5xx` on prod / payments: skipped

Execution `ae0648ee0a5e` · alarm `payments-5xx` · started 2026-10-27T18:33:40+00:00

| When | Who | What | Step | Detail |
|---|---|---|---|---|
| 18:33:40 | system | triggered |  | alarm payments-5xx |
| 18:33:40 | system | skipped |  | in cooldown (0:30:00) for ecs-service-5xx#prod/payments |

#### Errors with no recent deploy: scale-out waits for approval

### ⏸️ `ecs-service-5xx` on prod / orders: waiting_approval

Execution `70ded57c3a4d` · alarm `orders-5xx` · started 2026-10-28T04:25:40+00:00

Waiting for approval of **scale_out**: `opsctl runbook approve 70ded57c3a4d` or `opsctl runbook reject 70ded57c3a4d`

| When | Who | What | Step | Detail |
|---|---|---|---|---|
| 04:25:40 | system | triggered |  | alarm orders-5xx |
| 04:25:40 | system | check | service | {"cluster": "prod", "service": "orders", "running": 3, "desired": 3, "pending": 3, "task_definition": "arn:aws:ecs:ap-south-1:123456789012:… |
| 04:25:40 | system | approval requested | scale_out | {"cluster": "prod", "service": "orders", "current": 3, "factor": 2, "max": 8} |
| 04:25:40 | system | notified |  | Runbook ecs-service-5xx wants to run ecs_scale on prod / orders with {'cluster': 'prod', 'service': 'orders', 'current': 3, 'factor': 2, 'm… |

#### …approved by a person: checks re-run, then it scales

### ✅ `ecs-service-5xx` on prod / orders: succeeded

Execution `70ded57c3a4d` · alarm `orders-5xx` · started 2026-10-28T04:25:40+00:00

| When | Who | What | Step | Detail |
|---|---|---|---|---|
| 04:25:40 | system | triggered |  | alarm orders-5xx |
| 04:25:40 | system | check | service | {"cluster": "prod", "service": "orders", "running": 3, "desired": 3, "pending": 3, "task_definition": "arn:aws:ecs:ap-south-1:123456789012:… |
| 04:25:40 | system | approval requested | scale_out | {"cluster": "prod", "service": "orders", "current": 3, "factor": 2, "max": 8} |
| 04:25:40 | system | notified |  | Runbook ecs-service-5xx wants to run ecs_scale on prod / orders with {'cluster': 'prod', 'service': 'orders', 'current': 3, 'factor': 2, 'm… |
| 04:25:40 | vinod | approved | scale_out |  |
| 04:25:40 | system | check | service | {"cluster": "prod", "service": "orders", "running": 3, "desired": 3, "pending": 3, "task_definition": "arn:aws:ecs:ap-south-1:123456789012:… |
| 04:25:40 | vinod | action | scale_out | {"scaled_from": 3, "scaled_to": 6} |
| 04:25:40 | system | action | report | {"sent": false, "message": "orders on prod: rolled back=False, restarted=False, scaled=True."} |
