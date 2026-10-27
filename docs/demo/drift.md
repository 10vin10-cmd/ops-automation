## Drift: 7 resource(s)

🔴 3 critical · 🟠 2 high · 🟡 1 medium · ⚪ 1 low

| | Resource | Rule | What changed | Next step |
|---|---|---|---|---|
| 🔴 | `aws_security_group.orders_api` | public-ingress | `ingress[+]` added: **{"cidr_blocks": ["0.0.0.0/0"], "description": "debug", "from_port": 2…** | **revert automatically**: critical: Security group opened to the internet |
| 🔴 | `aws_s3_bucket_public_access_block.invoices` | public-bucket | `block_public_policy`: true → **false** | **revert automatically**: critical: S3 public access protection switched off |
| 🔴 | `aws_security_group.lab_scratch` | public-ingress | `ingress[0].cidr_blocks[0]`: 10.0.0.0/16 → **0.0.0.0/0** | **skipped (protected)**: protected by tag ops:drift=ignore |
| 🟠 | `aws_iam_role.payments_task` | iam-change | `managed_policy_arns[+]` added: **arn:aws:iam::aws:policy/AdministratorAccess** | **waiting for approval**: high: needs approval (iam-change) |
| 🟠 | `aws_cloudwatch_metric_alarm.payments_5xx` | resource-deleted | deleted | **waiting for approval**: high: needs approval (resource-deleted) |
| 🟡 | `aws_ecs_service.payments` | capacity-change | `desired_count`: 2 → **6** | **report**: may be intentional: codify it in the IaC or revert it |
| ⚪ | `aws_instance.reporting` | tags-only | `tags.cost-centre`: null → **cc-4410** | **report**: may be intentional: codify it in the IaC or revert it |

### Needs approval

Re-run with `--approve <address-or-rule>` to include these:

- `aws_iam_role.payments_task` (iam-change)
- `aws_cloudwatch_metric_alarm.payments_5xx` (resource-deleted)

### Codify or undo

These look deliberate. Update the code to match, or revert them:

- `aws_ecs_service.payments`: `desired_count` = 6
- `aws_instance.reporting`: `tags.cost-centre` = cc-4410

### Remediation (dry run)

```
terraform apply -input=false -auto-approve -target=aws_security_group.orders_api -target=aws_s3_bucket_public_access_block.invoices
```
