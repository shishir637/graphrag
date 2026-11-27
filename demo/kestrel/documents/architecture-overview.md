# Kestrel architecture overview

Kestrel runs on Kubernetes in AWS (ap-south-1, with a standby region in eu-west-1), operated by Platform and DevOps.

1. Every request enters through **api-gateway**, which checks the session with **auth-service**.
2. **kestrel-api** holds the product logic and stores data in PostgreSQL. **search-service** indexes workspaces
   with OpenSearch. **notification-service** sends email and push notifications.
3. **billing-service** charges customers through the payment provider every month or year and writes invoices.
4. **ingest-pipeline** collects product events; **warehouse-sync** copies product and billing data into the
   Snowflake warehouse every hour, where the warehouse_build pipeline builds the reporting tables.
5. **web-app** and **admin-console** (Frontend) and the **ios-app** and **android-app** (Mobile) call kestrel-api
   through api-gateway.

Data Engineering owns the warehouse. Finance reporting (MRR, ARR, churn) and the product analytics dashboards read
from it, so a stale warehouse means stale numbers for Finance and for customers' dashboards.
