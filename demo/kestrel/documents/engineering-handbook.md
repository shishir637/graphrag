# Kestrel Labs Engineering Handbook

Kestrel Labs builds Kestrel, a workflow analytics product sold as a subscription (Starter, Growth and Enterprise
plans). Engineering is led by Vikram Menon, Chief Technology Officer.

## Who owns which service

Every production service has one owning team. The owning team deploys it, carries its pager and writes the
incident review when it breaks.

| Service | Owning team | Team lead |
| --- | --- | --- |
| warehouse-sync | Data Engineering | Meera Krishnan |
| ingest-pipeline | Data Engineering | Meera Krishnan |
| api-gateway | Platform and DevOps | Ravi Shankar |
| auth-service | Platform and DevOps | Ravi Shankar |
| k8s-platform | Platform and DevOps | Ravi Shankar |
| kestrel-api | Backend | Daniel Okafor |
| billing-service | Backend | Daniel Okafor |
| notification-service | Backend | Daniel Okafor |
| search-service | Backend | Daniel Okafor |
| web-app | Frontend | Sara Lindqvist |
| admin-console | Frontend | Sara Lindqvist |
| ios-app | Mobile | Arjun Mehta |
| android-app | Mobile | Arjun Mehta |

## How we ship

All changes reach production through the CI/CD pipeline run by Platform and DevOps. A change needs one approving
code review and a green build. Deploys run on working days between 09:00 and 19:00 IST; deploys outside that
window need approval from the on-call Site Reliability Engineer.

Billing-service and auth-service changes need a second reviewer from the owning team, because mistakes there cost
customers money or lock them out.

## What we measure (DORA)

We track four delivery measures for every team, from the deployments and incidents tables:

- **Deployment frequency:** production deploys per week. Target: at least daily for kestrel-api and web-app.
- **Lead time for changes:** hours from the first commit of a change to its production deploy. Target: under 24 hours.
- **Change failure rate:** share of production deploys that fail or are rolled back. Target: under 15 percent.
- **Time to restore:** hours from an incident being opened to service being restored. Target: under 4 hours for Sev1.

Quality reviews happen monthly. The engineering leads review the numbers with the CTO.
