# Incident review INC-2026-031: Customers charged twice for annual renewals

| Fact | Value |
| --- | --- |
| Severity | Sev1 |
| Service | billing-service (owned by Backend) |
| Opened | 2026-03-14 09:20 IST |
| Restored | 2026-03-14 14:50 IST |
| Time to restore | 5.5 hours |
| Incident owner | Daniel Okafor |

## What happened

Customers charged twice for annual renewals. It started after deploy D003006 of billing-service at 2026-03-10 16:23.

## Why it happened

The root cause was that a retry in the renewal job sent the charge request twice when the payment provider answered slowly.

## Follow-up actions

1. Make renewal charges idempotent with a charge key (Backend, Daniel Okafor)
2. Alert when one customer is charged twice within a day (Finance and Backend)
3. Refund the 64 affected customers and email them (Customer Success, Leela Iyer)
