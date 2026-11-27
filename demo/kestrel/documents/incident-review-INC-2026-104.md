# Incident review INC-2026-104: Dashboards stale for 14 hours after warehouse sync stalled

| Fact | Value |
| --- | --- |
| Severity | Sev2 |
| Service | warehouse-sync (owned by Data Engineering) |
| Opened | 2026-10-21 06:10 IST |
| Restored | 2026-10-21 20:10 IST |
| Time to restore | 14.0 hours |
| Incident owner | Meera Krishnan |

## What happened

Dashboards stale for 14 hours after warehouse sync stalled. It started after deploy D004363 of warehouse-sync at 2026-10-05 13:15.

## Why it happened

The root cause was that a schema change in the events table made the warehouse sync stall without failing, so the warehouse_build pipeline used 14-hour-old data.

## Follow-up actions

1. Fail warehouse sync loudly on schema mismatches (Data Engineering)
2. Alert when data freshness is over 3 hours (Data Engineering, Meera Krishnan)
3. Add the events schema to the release checks (QA, Fatima Sheikh)
