# Getting started

Context Layer gives AI apps (Claude, ChatGPT, Cursor, your own bots) governed access to your documents,
business metrics and a knowledge graph over one MCP link. It runs on your own computer or server and has
no AI model inside, so it needs no API key.

About 30 minutes, most of it the first download.

## 1. What you need

- **Docker Desktop** (Windows or macOS) or Docker Engine (Linux).
  - On Windows, virtualization has to be on. If Docker says it isn't, turn on "Virtualization"
    ("Intel VT-x" or "SVM") in the BIOS, then run `wsl --install` once.
- 16 GB RAM and 15 GB free disk. The local models are about 5 GB.
- Git, or download this repository as a ZIP from GitHub (Code → Download ZIP).

## 2. Start it

```bash
git clone <this repository URL> context-layer
cd context-layer
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and set `CL_DB_PASSWORD`, `CL_QDRANT_API_KEY` and `CL_SECRET_KEY` (the key that encrypts stored
tokens; the comment in `.env.example` shows how to make one). Then:

```bash
docker compose up -d --build
```

The first build and start take 10-20 minutes while the models download. Check progress with
`docker compose logs -f app` (builds run in the `worker` service: `docker compose logs -f worker`). When `http://localhost:8080/api/v1/health` shows `"ok": true`, it's running.

## 3. Set it up in the browser

1. Open http://localhost:8080 and create the **owner** account (your email and a password of 10+ characters).
2. **Documents**: upload PDF, Word, PowerPoint, Excel, CSV, JSON, Markdown, HTML or text files, then click
   **Prepare documents**.
3. **Data tables** (optional): upload CSV, Excel, JSON or Parquet, then click **Prepare data**. Context Layer
   drafts metric definitions; review and **Approve** them on the **Metrics** page.
4. **Knowledge graph** (optional): click **Build graph**. "Accurate" mode takes about 1-2 seconds per passage
   on a laptop; "fast" mode (Settings) is about 20 times quicker.
   For better links ("X heads Y", "Z owns W"), turn on **AI extraction** in Settings: choose Claude or an
   OpenAI-compatible API with your own key, or a local model such as Ollama (no key; documents stay on your
   network). Click **Test connection**, set graph quality to **ai**, then **Build graph**. It is used only for
   building the graph; answering never calls an AI model.
5. **Playground**: ask a question to check what agents will get back.
6. **Access**: add team members. Viewers see only the access groups you give them; owners and admins see everything.

## 4. Connect an AI app

### On the same computer, with a key (Claude Code, Cursor, VS Code, scripts)

Go to **Agents → Connect an agent**, choose what it may read, and copy the ready-made setup for your app.
For example, with Claude Code:

```bash
claude mcp add --transport http context-layer http://localhost:8080/mcp --header "Authorization: Bearer <key>"
```

### Claude or ChatGPT on the web, by signing in (no key)

Claude and ChatGPT connect from the internet, so Context Layer needs a public HTTPS address. Only the MCP
link, its sign-in pages and one-time upload links are published. The admin web app stays private.

**Quick test (address changes on every restart):**

1. In `.env`, set `CL_PUBLIC_HOSTS=*.trycloudflare.com`.
2. Run:
   ```bash
   docker compose --profile quicktunnel up -d
   ```
3. The public link appears under **Settings → Public MCP link**, for example
   `https://random-words.trycloudflare.com/mcp`.

**To keep one fixed address:** this needs a free Cloudflare account and a domain on Cloudflare.

1. In Cloudflare, go to Zero Trust → Networks → Tunnels and create a tunnel.
2. Add a public hostname, for example `contextlayer.yourdomain.com`, pointing to `http://app:8080`.
3. Copy the tunnel token.
4. In `.env`, set:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=<token>
   CL_PUBLIC_HOSTS=contextlayer.yourdomain.com
   CL_PUBLIC_URL=https://contextlayer.yourdomain.com
   ```
5. Run:
   ```bash
   docker compose --profile tunnel up -d
   ```

Then each person:

- **Claude**: Settings → Connectors → Add custom connector, then paste the public `/mcp` link.
- **ChatGPT**: turn on developer mode, create an app with the public `/mcp` link, and choose OAuth sign-in.
- A Context Layer sign-in page opens. They sign in with their Context Layer email and password, then click **Allow**.

Now they can ask questions in their own Claude or ChatGPT; the AI runs on their own plan. Owners and admins
can also attach a file and say "add this to Context Layer". It builds in the background, and the chat answers
from it once the build is ready.

## 5. Day to day

| Task | Where |
|---|---|
| Add or remove documents | Documents page, or attach in the chat (owners and admins) |
| Who sees what | Access (groups for people), Agents (groups for keys) |
| Disconnect someone's Claude or ChatGPT | Agents → Signed-in apps → Disconnect |
| See every question agents asked | Activity |
| Check search and metric accuracy | Quality |
| Update to a new version | `git pull` then `docker compose up -d --build` |
| Back up | the Docker volumes `app_data`, `documents`, `tables`, `semantic_layer`, `graph_data` (the others are rebuilt) |

### Business context from chat, personal layers and sharing (v0.6)

- **Change definitions from chat.** Anyone can say things like "count only rolled-back deploys as failures",
  "our fiscal year starts in April" or "'platform team' means Platform and DevOps". Claude first asks
  "Are you sure?" (it says whether the company or you define the item now, and how a number changes), then
  saves it in **your own layer**: only your answers change. Every change is recorded with your name;
  "undo that" or "who changed failed deployments?" work from chat, and admins can undo on the Layers page.
- **Company layer.** Owners, admins and publishers can make a change the company definition
  ("publish my failed deployments definition to the company").
- **Share your layer.** "Share my layer with the Data Engineering team" (or named people, or everyone) gives a
  link. People add the link as a connector and sign in; only people on the share list get in. In their normal
  connector they can also say "use Shishir's layer". Members can share only with people who already have an
  account; admins sharing with someone new get an invite link to pass on.
- **People and teams** live on the Directory page (CSV import, invites). Teams are access groups too.
- **Demo company:** Settings → Demo company loads Kestrel Labs (a made-up software company) as the company
  layer. Your personal layers stay; the old company layer is saved first under `var/backups`.

## 6. If something goes wrong

- **Page won't open**: run `docker compose ps`. If `app` isn't healthy, check `docker compose logs app --tail 50`.
- **Claude says it can't connect**: open the public `/mcp` link in a browser. "Sign in to Context Layer from
  your AI app" (401) means it's reachable. A quick-tunnel address changes after a restart, so people have to add
  the new one. Use a named tunnel to avoid this.
- **Claude shows "Bad Gateway" right after a restart**: wait a few minutes and try again; it clears by itself
  (the requests never reached Context Layer, which was already answering).
- **421 "Unknown host name"**: add that name to `CL_PUBLIC_HOSTS` in `.env`, then run `docker compose up -d`.
- **Build graph takes long**: switch to fast mode in Settings, or leave it running. Answers keep working while it builds.
- **Running without Docker** (development): see the README.
