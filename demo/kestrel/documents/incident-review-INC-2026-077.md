# Incident review INC-2026-077: API gateway outage after certificate rotation

| Fact | Value |
| --- | --- |
| Severity | Sev1 |
| Service | api-gateway (owned by Platform and DevOps) |
| Opened | 2026-08-02 02:45 IST |
| Restored | 2026-08-02 05:00 IST |
| Time to restore | 2.25 hours |
| Incident owner | Ravi Shankar |

## What happened

API gateway outage after certificate rotation. It was not caused by a deploy.

## Why it happened

The root cause was that an expired intermediate certificate was not included when certificates were rotated, so clients could not complete TLS handshakes.

## Follow-up actions

1. Automate certificate rotation and check the full chain before switching (Platform and DevOps)
2. Page the on-call 14 days before any certificate expires (Platform and DevOps)
