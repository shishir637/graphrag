# Context Layer: product brief (v0.1, self-hosted)

> 0.5: one server hosts many workspaces (sign-up or invite); each connects sources once (uploads, GitHub, Postgres)
> that are then kept up to date on their own. 0.7: one home for every piece of data (landing zone, Postgres,
> rebuildable engines; ARCHITECTURE.md), MySQL and S3-compatible folders, separate API and worker containers.
> See CHANGES.md.

## Who it is for

**The buyer and daily user is a company's data / IT admin**: the person asked to "let our AI agents use
company knowledge, safely". Their company already uses Claude, ChatGPT, Copilot, Cursor or its own bots.

What goes wrong for them today:

| Pain | What they need from the product |
|---|---|
| Agents make up company facts, or quote outdated files | Answers grounded in the company's own documents, always with sources |
| Agents and dashboards give different numbers for "revenue" | One reviewed definition per business number (semantic layer), used by every agent |
| No control over which agent sees HR, finance or customer data | Per-agent keys limited to access groups, enforced before anything reaches a model |
| No record of what an agent asked or was shown | Activity and audit log per agent and per admin action (DPDP, internal audit) |
| Every team builds its own RAG prototype | One shared layer, one link (MCP) any agent can connect to |
| Data cannot leave the company | Runs on the company's own server; works with Claude or a local model |
| "Is it any good?" | Built-in quality checks with scores they can show their manager |

## What the product does

```
Sources ──► Context Layer (on the customer's server) ──► one MCP link per agent ──► Claude, ChatGPT, Cursor, own bots
 documents      search (hybrid, re-ranked)              keys = access groups + capabilities
 data tables    semantic layer (metrics, no SQL by LLM)  every call logged
                knowledge graph (connections, overview)
                router picks the right one per question
```

## Screens (left navigation)

| Screen | What the admin does there |
|---|---|
| **Overview** | Setup checklist until everything is ready; then health (sources, index, graph, AI model), agent calls this week, recent problems |
| **Sources → Documents** | Upload files or point at a folder, set access groups per file, see status (ready / processing / failed) and when each was last indexed |
| **Sources → Data** | Upload tables (CSV, Excel, Parquet), see profiling (rows, columns, quality checks) |
| **Knowledge → Metrics** | The reviewed business definitions: metrics, dimensions, synonyms, owner, status. Read-only here; changed by review |
| **Knowledge → Graph** | Browse entities and how they connect; search an entity; see the passages behind each link |
| **Agents** | One card per connected agent: what it may read, last used, calls; create a key, copy the link and setup for each client, revoke |
| **Playground** | Ask a question *as an agent* and see the route taken (documents, data or graph), the answer, sources and timings |
| **Activity** | Every agent call and every admin action, filterable, exportable to CSV |
| **Quality** | Run the test sets (documents, data, graph) and see scores over time |
| **Access** | Access groups (which files and agents use each) and team members with roles |
| **Settings** | AI model (Claude key, local model via Ollama, or none), storage status, data folder, version |

Roles: **Owner** (everything, including team and settings), **Admin** (sources, agents, quality),
**Viewer** (read-only, Playground allowed).

Agent keys carry **access groups** (which documents) and **capabilities**: documents, data (metrics),
knowledge graph. A support bot can get documents in `public, support` and nothing else; a finance agent
can get `finance` documents plus metrics.

## Packaging

- One `docker compose up` brings up the app, a worker, Postgres (the official image) and Qdrant; models are downloaded on
  first use and cached in a volume. Windows Application Control does not apply inside the container.
- All data stays in the customer's volumes. Outbound traffic only to the AI provider they configure
  (none with a local model).
- Every component is open source under permissive licences (no GPL in the shipped code path).

## Version 0.1 scope

In: everything in the screens table, single organisation per installation, local login with roles,
documents + data + graph, MCP over HTTP with bearer keys, activity and audit, CSV export.

Next: single sign-on (OIDC), scheduled re-indexing of folders, connectors (Google Drive, SharePoint,
Postgres), editing metrics in the app with review, hosted (multi-tenant) edition, store apps as
companions to the console.

## Decisions for the team

1. How it is sold: per-installation annual licence with support, support/onboarding contracts on an
   open-source core, or hosted later. (Anand: open source first.)
2. Product name and domain.
3. First pilot customer and their first two sources.
