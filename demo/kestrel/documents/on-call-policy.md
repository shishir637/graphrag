# On-call and incident policy

Platform and DevOps (lead: Ravi Shankar) runs incident response for Kestrel Labs. Every engineering team with a
production service has a weekly on-call rotation; Platform and DevOps is the second line for all of them.

## Severity levels

| Severity | Meaning | First response | Who is paged |
| --- | --- | --- | --- |
| Sev1 | Kestrel is down or customers lose money or data | 15 minutes | Owning team on-call, Platform on-call, the CTO |
| Sev2 | A major feature is broken for many customers | 30 minutes | Owning team on-call, Platform on-call |
| Sev3 | A feature is degraded or broken for a few customers | Next working hours | Owning team |
| Sev4 | Cosmetic or minor | Planned work | Owning team |

## After an incident

Every Sev1 and Sev2 incident gets a written incident review within five working days, written by the incident
owner and reviewed by the owning team's lead. Reviews are blameless. Each review lists follow-up actions with
owners; Platform and DevOps tracks them until they are done.

Customer Success (lead: Leela Iyer) informs affected customers for every Sev1 incident, and for Sev2 incidents
that last more than two hours.
