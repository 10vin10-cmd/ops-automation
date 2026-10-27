## Cost and hygiene: 9 finding(s), ~$112.26/month

| Finding | Resource | Detail | Est. $/month | Cleanup |
|---|---|---|---:|---|
| unattached-volume | `vol-42b6664b319b722c7` | 500 GiB gp3, unattached, created 120 days ago | 45.60 | would: snapshot, then delete |
| old-snapshot | `snap-32f8c63c70fae9c24` | 500 GiB, 120 days old, not used by an AMI | 25.00 | would: delete |
| idle-load-balancer | `legacy-admin` | application load balancer with no targets | 18.40 | report only |
| unattached-volume | `vol-699b9c9d33750904e` | 100 GiB gp2, unattached, created 120 days ago | 11.40 | would: snapshot, then delete |
| unattached-volume | `vol-04df0252708d8b072` | 50 GiB gp3, unattached, created 120 days ago | 4.56 | skipped: tagged ops:keep=true |
| idle-elastic-ip | `eipalloc-8a6e8731624bd6f5b` | 127.86.87.117 not associated | 3.65 | would: release |
| idle-elastic-ip | `eipalloc-a11f4a5e8f1ce0d1a` | 127.33.191.89 not associated | 3.65 | would: release |
| missing-tags | `i-17b966f568496cdb5` | missing cost-centre | 0.00 | report only |
| stopped-instance | `i-17b966f568496cdb5` | m5.large stopped 120 days ago; its volumes still bill | 0.00 | report only |

Estimates use list prices from the config; they size the opportunity, not the bill.
