# Zendesk MCP Server — Remote Deployment Architecture Plan

**Author:** Chief Architect review
**Date:** 2026-07-24
**Status:** Proposed
**Scope:** Upgrade the stdio-only Zendesk MCP server to a production remote MCP server (Streamable HTTP + OAuth 2.1) deployed on AWS EC2, usable by any MCP-capable AI application.

---

## 1. Current State Assessment (verified against code)

| Capability | Status | Evidence |
|---|---|---|
| stdio transport | ✅ Present | `server.py` → `stdio_server()` only |
| SSE transport | ❌ Absent | No SSE app/route anywhere |
| Streamable HTTP transport | ❌ Absent | No HTTP server at all |
| Authentication (any) | ❌ Absent | Server trusts its caller; only outbound Zendesk Basic auth exists |
| OAuth 2.1 / token validation | ❌ Absent | — |
| Multi-tenant support | ❌ Absent | Single global `ZendeskClient` built from `.env` at import time |
| Async-safe I/O | ❌ Absent | Sync Zenpy/requests calls inside async handlers block the event loop |
| Health endpoint / observability | ❌ Absent | Logging only |
| SDK version | ⚠️ Old | `mcp>=1.1.2`, low-level `Server` API; predates Streamable HTTP support |

**Conclusion: what you want does NOT exist in this codebase. It must be implemented.**

Important context from the ecosystem research:

- **SSE as a standalone transport is deprecated** (since MCP spec 2025-03-26). Do **not** build the old HTTP+SSE dual-endpoint transport. Build **Streamable HTTP** (single `/mcp` endpoint; POST JSON-RPC in, JSON or SSE stream out). Optionally keep a legacy `/sse` endpoint for old clients — vendors are removing SSE through mid-2026.
- **OAuth 2.1 is mandatory** for internet-facing MCP servers per the current spec (2025-06-18 and the 2026-07-28 release candidate). The MCP server acts as an **OAuth 2.1 Resource Server**: it validates Bearer tokens, and MUST publish **Protected Resource Metadata (RFC 9728)** at `/.well-known/oauth-protected-resource` so clients auto-discover the authorization server. PKCE (S256) is required; implicit grant is banned.
- The 2026-07-28 spec RC adds **Client ID Metadata Documents (CIMD)** as the successor to Dynamic Client Registration — plan for it, but DCR support still maximizes client compatibility today.

---

## 2. Target Architecture

```
                         Internet (any MCP client: Claude, ChatGPT, Cursor, custom agents)
                                        │  HTTPS (TLS 1.2+)
                                        ▼
                        ┌─────────────────────────────┐
                        │  Route 53 (mcp.yourco.com)  │
                        └──────────────┬──────────────┘
                                       ▼
                 ┌────────────────────────────────────────┐
                 │ ALB (ACM cert)  — or Caddy/Nginx on EC2 │
                 │  • TLS termination                     │
                 │  • AWS WAF (rate limit, IP rules)      │
                 │  • Health checks → /health             │
                 └──────────────┬─────────────────────────┘
                                ▼  (private subnet)
        ┌───────────────────────────────────────────────────────┐
        │ EC2 instance — Docker                                 │
        │  ┌─────────────────────────────────────────────────┐  │
        │  │ zendesk-mcp-server (FastMCP / Streamable HTTP)  │  │
        │  │  /mcp        — MCP endpoint (POST/GET/DELETE)   │  │
        │  │  /health     — liveness/readiness               │  │
        │  │  /.well-known/oauth-protected-resource (RFC9728)│  │
        │  │  Auth middleware: JWT validation (issuer, aud,  │  │
        │  │    exp, scopes) — tokens from Cognito/Auth0     │  │
        │  └─────────────────────────────────────────────────┘  │
        └───────────────────────────────────────────────────────┘
                                │ HTTPS (outbound)
                                ▼
                     Zendesk API (subdomain.zendesk.com)
        ┌───────────────────────────────────────────────────────┐
        │ Authorization Server (do NOT build your own):         │
        │   Amazon Cognito | Auth0 | WorkOS | Descope | Keycloak│
        │   • OAuth 2.1 + PKCE (S256)                           │
        │   • Dynamic Client Registration (RFC 7591)            │
        │   • Authorization Server Metadata (RFC 8414)          │
        └───────────────────────────────────────────────────────┘
```

Key decision: **the MCP server is only a Resource Server.** Delegate the Authorization Server role to a managed IdP (Cognito is the natural AWS choice; Auth0/Descope/WorkOS have first-class MCP support including DCR, which Cognito lacks natively — see §5 trade-offs).

---

## 3. Workstream 1 — Transport upgrade (Streamable HTTP)

### 3.1 Migrate to FastMCP

The current code uses the low-level `mcp.server.Server` API from an old SDK. Rewrite on **FastMCP** (either `mcp.server.fastmcp` in the official SDK ≥1.8, or the standalone `fastmcp` 3.x package — recommended for its built-in auth providers).

- `mcp.run(transport="http", host="0.0.0.0", port=8000)` gives the single `/mcp` Streamable HTTP endpoint with SSE upgrade for streaming responses.
- Decorator-based tools with typed signatures replace the hand-written JSON schemas (`@mcp.tool`), eliminating schema drift between `list_tools` and `call_tool`.
- Keep the two prompts (`@mcp.prompt`) and the knowledge-base resource (`@mcp.resource("zendesk://knowledge-base")`).
- **Keep stdio mode working** (flag/env: `MCP_TRANSPORT=stdio|http`) so local Claude Desktop users are not broken.

### 3.2 Fix blocking I/O (prerequisite for HTTP concurrency)

Under stdio one blocked event loop is invisible; under HTTP with N concurrent clients it's fatal.

- Wrap all Zenpy/requests calls with `await anyio.to_thread.run_sync(...)` (or convert the client to `httpx.AsyncClient` — larger but cleaner rewrite; Zenpy is sync-only, so thread offloading is the pragmatic path).
- Make tool handlers `async def` and offload; set a bounded thread pool.

### 3.3 Session & scaling behavior

- Streamable HTTP sessions are negotiated via the `Mcp-Session-Id` header — FastMCP handles this. For a single EC2 instance nothing more is needed. If you later scale horizontally, either enable ALB sticky sessions or run **stateless mode** (`stateless_http=True`), which the latest spec direction favors (plain round-robin, no session store).
- Support `DELETE /mcp` for explicit session termination and honor `Origin` validation (DNS-rebinding protection — FastMCP does this by default; keep it on).

**Deliverables:** refactored `server.py` (FastMCP), `transport` config, async-safe client, parity tests stdio vs HTTP.
**Estimate:** 3–5 dev-days.

---

## 4. Workstream 2 — Authentication & Authorization (OAuth 2.1)

### 4.1 Spec-required pieces (MUST)

1. **Bearer token validation** on every `/mcp` request: signature (JWKS), `iss`, `aud` (token must be issued *for this MCP server* — reject tokens for other resources: prevents confused-deputy/token-passthrough), `exp`, scopes.
2. **RFC 9728 Protected Resource Metadata** at `/.well-known/oauth-protected-resource`, advertising the authorization server URL and supported scopes. Return `401` + `WWW-Authenticate` header pointing to it — this is how Claude/ChatGPT auto-start the OAuth flow.
3. **PKCE S256** enforced at the AS; no implicit grant; short-lived access tokens + refresh token rotation.

### 4.2 Scope model (proposal)

| Scope | Tools |
|---|---|
| `zendesk:tickets:read` | get_ticket, get_tickets, get_ticket_comments, get_ticket_attachment |
| `zendesk:tickets:write` | create_ticket, update_ticket, create_ticket_comment |
| `zendesk:kb:read` | knowledge-base resource |

Enforce per-tool scope checks in middleware; return proper `-32603`/403 errors. Also mark tools with **annotations** (`readOnlyHint`, `destructiveHint`) so clients can gate confirmation UX.

### 4.3 Implementation options (pick one)

| Option | Effort | DCR support | Notes |
|---|---|---|---|
| **FastMCP `JWTVerifier` + Auth0/Descope/WorkOS** ⭐ recommended | Low | Yes | These IdPs support DCR out of the box → works instantly with Claude web/desktop connectors |
| FastMCP `OAuthProxy` + Cognito | Medium | Proxied | Cognito lacks DCR; FastMCP's OAuth Proxy bridges it |
| API Gateway + Cognito authorizer in front of EC2 | Medium | No | AWS-native, but MCP client compatibility suffers without DCR/RFC9728 care |
| Static Bearer tokens / API keys | Trivial | n/a | Acceptable only for a private beta; NOT spec-compliant for public exposure |

### 4.4 Multi-tenancy decision (you must decide)

Today one `.env` = one Zendesk account = **your** credentials serve every caller. If "anyone can connect":

- **Phase 1 (shared tenant):** all authenticated users hit your Zendesk instance. Simple; acceptable for your own org's support.
- **Phase 2 (per-user Zendesk identity):** map the OAuth subject to per-tenant Zendesk credentials (stored in AWS Secrets Manager / DynamoDB), or pass through Zendesk's own OAuth. Required if third parties should connect *their* Zendesk accounts. This is the biggest architectural fork in the whole plan — decide early.

**Deliverables:** auth middleware, RFC 9728 endpoint, IdP configuration, scope enforcement, tenant strategy doc.
**Estimate:** 4–8 dev-days (Phase 1).

---

## 5. Workstream 3 — AWS EC2 Deployment

### 5.1 Infrastructure (Terraform or CDK — do not click-ops)

- **VPC:** EC2 in a private subnet; ALB in public subnets. Egress via NAT for Zendesk API calls.
- **EC2:** t3.small is plenty to start (I/O-bound workload). Amazon Linux 2023 + Docker; run the existing hardened Dockerfile. `systemd` unit or `docker compose` with `restart: always`.
- **ALB + ACM:** TLS 1.2+ cert for `mcp.yourdomain.com`; HTTP→HTTPS redirect; idle timeout ≥ 300 s (SSE streams are long-lived); health check on `/health`.
  - *Budget alternative:* skip ALB, run **Caddy** on the instance for auto-TLS (Let's Encrypt). Cheaper (~$16/mo saved), less HA.
- **Security Group:** ALB→EC2 :8000 only; SSH via SSM Session Manager (no port 22 open).
- **WAF on ALB:** rate limiting, AWS managed rule sets, geo/IP rules as needed.
- **Secrets:** Zendesk credentials in **AWS Secrets Manager** (or SSM Parameter Store), injected at boot via instance role — remove `.env`-on-disk in production.
- **Observability:** CloudWatch agent → logs + metrics; alarms on 5xx rate, health-check failures, CPU. Structured JSON logging in app (request id, tool name, caller sub, latency).

### 5.2 CI/CD

Extend the existing GitHub Actions CI: build image → push to ECR → SSM RunCommand / CodeDeploy pull-and-restart on EC2. Tag images by git SHA; keep one-command rollback.

### 5.3 Roadmap note

EC2 is fine to start. AWS's own guidance for MCP servers favors **ECS Fargate behind ALB** (or Lambda for low traffic) — a natural second step since you'll already be containerized; the Terraform delta is small.

**Estimate:** 3–5 dev-days including IaC and CI/CD.

---

## 6. Workstream 4 — Features you didn't ask for but should have (2026 trend analysis)

Ranked by value:

1. **Zendesk Search tool** (`search_tickets` via `/api/v2/search.json`) — the single most useful missing tool; agents constantly need "find tickets about X / from user Y / status open".
2. **Structured output** — declare `outputSchema` on tools and return `structuredContent` (spec 2025-06-18+). FastMCP does this automatically from return-type annotations. Big quality win for downstream agents.
3. **Tool annotations** — `readOnlyHint` / `destructiveHint` / `idempotentHint` on every tool; clients use these for confirmation prompts and parallelization.
4. **Elicitation** (spec 2025-06-18+) — e.g., `create_ticket_comment` with `public=true` can elicit user confirmation before posting publicly to a customer. Strong safety story.
5. **Pagination & context hygiene** — replace deprecated Zendesk offset pagination with **cursor pagination** (`page[size]`/`page[after]`); paginate the knowledge base (a `search_articles` tool instead of the current dump-everything resource, which can blow the client's context window).
6. **Progress notifications** for long operations (KB fetch) via `ctx.report_progress` — visible spinners in clients over Streamable HTTP.
7. **Users/organizations tools** (`get_user`, `search_users`) — ticket workflows constantly need requester/assignee resolution; today the model only sees numeric IDs.
8. **Rate limiting & retries** — respect Zendesk 429/`Retry-After`; add per-caller rate limits at WAF and in-app.
9. **MCP Registry publication** — publish to the official MCP registry (now the "app store" for servers; moving under Linux Foundation governance) for discoverability, with `server.json` metadata.
10. **Security hardening for tool poisoning era** — pin dependency versions, output-sanitize ticket content (ticket bodies are untrusted user input that flows into LLM context — document this; consider stripping HTML/scripts from `html_body`), audit-log every write tool call.
11. **Tests** — none exist. Add pytest + `fastmcp.Client` in-memory tests for every tool, plus an HTTP integration smoke test in CI.

---

## 7. Phased Roadmap

| Phase | Content | Duration | Exit criteria |
|---|---|---|---|
| **0. Foundation** | FastMCP migration, async-safe client, tests, stdio parity | ~1 wk | All tools pass tests on stdio + local HTTP |
| **1. Remote core** | Streamable HTTP, /health, Docker, EC2+ALB+TLS via IaC, Bearer-token gate (private beta) | ~1 wk | Claude connects via `https://mcp.yourdomain.com/mcp` |
| **2. Spec-compliant auth** | OAuth 2.1 RS: RFC 9728 metadata, JWT validation, scopes, IdP w/ DCR | ~1–1.5 wk | Claude/ChatGPT complete OAuth flow unassisted; scope enforcement verified |
| **3. Feature uplift** | search tools, structured output, annotations, elicitation, cursor pagination, progress | ~1–2 wk | New tools live; registry-ready |
| **4. Scale & polish** | Registry publication, multi-tenant decision, ECS/Fargate migration path, WAF tuning, dashboards | ongoing | — |

Total to public-ready: **4–6 weeks** of focused part-time work.

---

## 8. Top Risks

1. **Multi-tenancy ambiguity** — "anyone can connect" with a single shared Zendesk credential means strangers read/write *your* tickets. Decide §4.4 before Phase 2, and until then restrict token issuance to known users.
2. **Old SDK / big-bang rewrite** — FastMCP migration touches every handler; mitigate with the parity test suite in Phase 0.
3. **Auth provider choice lock-in** — Cognito is AWS-native but DCR-less; Auth0/Descope free tiers may be sufficient and are far smoother for MCP clients. Prototype the Claude connector flow against your chosen IdP in week 1 of Phase 2.
4. **Context/token blowout** — the KB resource and unpaginated comment dumps can exceed client budgets; fix in Phase 3.
5. **Spec churn** — 2026-07-28 spec (CIMD, etc.) lands imminently; building on FastMCP/official SDK insulates you since they track the spec.

---

## 9. Immediate Next Actions

1. Approve IdP choice (Cognito+proxy vs Auth0/Descope/WorkOS).
2. Decide tenancy model (shared vs per-user Zendesk credentials).
3. Confirm domain name + AWS account for deployment.
4. Start Phase 0 (FastMCP migration + tests) — no external dependencies, can begin today.

---

## 10. Addendum (2026-07-24) — Review round 2

### 10.1 Zendesk API coverage gap analysis

Current tools cover only core Tickets CRUD + comments + attachment download + a KB dump. Full gap matrix, tiered by value:

**Tier 1 — high value, implement in Phase 3:**

| Missing capability | Zendesk API | Why it matters |
|---|---|---|
| Ticket/user/org search | `/api/v2/search.json` | The #1 agent need: "find tickets about X, from Y, status open" |
| Users (get/search) | `/api/v2/users` | Today the model only sees numeric requester/assignee IDs — cannot resolve names/emails |
| Ticket Fields metadata | `/api/v2/ticket_fields` | `custom_fields` are opaque `{id, value}` pairs without this; the model can't read or set them meaningfully |
| Groups & assignment | `/api/v2/groups` | Route tickets to teams, not just individuals |
| Attachment **upload** | `/api/v2/uploads.json` | We can download but not attach files to comments |
| KB article search | `/api/v2/help_center/articles/search` | Replaces the dump-everything resource; context-safe |

**Tier 2 — workflow depth:**

| Capability | API | Notes |
|---|---|---|
| Macros (list + apply) | `/api/v2/macros` | Lets agents apply canned org workflows |
| Views (list + execute) | `/api/v2/views` | How support teams actually organize queues |
| Organizations | `/api/v2/organizations` | B2B support context |
| Ticket audits/metrics | `/api/v2/tickets/{id}/audits`, `/ticket_metrics` | SLA, first-reply time, full history — reporting/analysis prompts |
| Satisfaction ratings (CSAT) | `/api/v2/satisfaction_ratings` | Quality analysis |
| Bulk update + job status | `/api/v2/tickets/update_many`, `/job_statuses` | "Close all solved tickets older than 30 days" |

**Tier 3 — situational / admin:**
Side conversations, ticket merge/delete, suspended tickets, tags listing, Help Center write (create/update articles), webhooks/triggers/automations admin, Talk/Chat/Sell APIs. Recommend **excluding** admin-config APIs (triggers/automations) from an internet-exposed server — too destructive.

Also note: `post_comment` currently does a wasteful full ticket fetch before update, and `get_ticket_comments` has no pagination (a 500-comment ticket will blow the context window) — fix both in Phase 0.

### 10.2 Docker deployment (confirmed in scope)

A Dockerfile already exists but is stdio-oriented. Changes:

- Run HTTP transport by default in the image: `MCP_TRANSPORT=http`, `EXPOSE 8000`, `HEALTHCHECK CMD curl -f http://localhost:8000/health`.
- Multi-stage build (uv → slim runtime), keep the non-root user, pin base image digest.
- Add `docker-compose.yml` for single-box EC2 deployment: `zendesk-mcp` + `caddy` (auto-TLS) services, secrets via env/Secrets Manager — this *is* the EC2 deployment unit from §5.
- CI: build → push ECR → tag by git SHA (already planned in §5.2).
- Keep a stdio-compatible invocation (`docker run -i ... zendesk-mcp stdio`) so the existing Claude Desktop docs still work.

### 10.3 Auth: hybrid model — simple internal first, OAuth optional

Decision: implement **two auth modes behind one middleware**, selected by config:

1. **Internal API keys (Phase 1, default):**
   - Admin generates opaque keys (256-bit random, prefix `zmk_`); server stores only SHA-256 hashes in SQLite/DynamoDB with: name, scopes (`tickets:read`, `tickets:write`, `kb:read`), created/expires, last-used, revoked flag.
   - Clients send `Authorization: Bearer zmk_...`. Claude Code/Desktop, Cursor, and most MCP clients support custom headers for HTTP servers — this works today with zero IdP.
   - Constant-time hash comparison, per-key rate limits, audit log of every call.
   - Limitation to document: **no auto-discovery** — Claude.ai web "Connectors" UI expects the OAuth flow (RFC 9728), so browser-based clients that refuse header auth need mode 2.
2. **OAuth 2.1 via external IdP (Phase 2, optional flag):** as per §4 — enables "anyone can connect" with self-service onboarding.

Do **not** build a homegrown OAuth authorization server (token issuance, PKCE, consent screens) — that's the one component where DIY reliably becomes a security liability. Internal = simple API keys; standard = delegated IdP.

### 10.4 Web admin interface — yes, with guardrails

Verdict: **good idea**, valuable for exactly the two uses proposed (Zendesk connection config + API key management), and it becomes the tenant-management console if multi-tenancy lands later. Conditions:

- **Don't edit `.env` on disk.** Runtime-mutable config belongs in a small store (SQLite on the instance, or DynamoDB + Secrets Manager for the Zendesk token). App holds config in memory and hot-swaps the `ZendeskClient` on change ("test connection" button before save). `.env` remains a bootstrap fallback for local dev.
- **Isolate it from the MCP surface:** serve `/admin` on a separate port (e.g., 9000) that the ALB does **not** forward — reachable only via SSM port-forward or IP-allowlisted path. The public internet should never see the admin UI.
- **Own auth, stronger than the MCP keys:** single admin account with strong password + TOTP 2FA (or just Cognito-hosted login for the admin app only). Session cookies: `Secure`, `HttpOnly`, `SameSite=Strict`, CSRF tokens.
- Never render the stored Zendesk token back; show `••••` + "replace" only. Show API keys once at creation.
- Audit log for every admin action; keep it append-only.
- Tech: FastAPI + a few HTMX/Jinja templates mounted alongside FastMCP in the same ASGI app (separate port binding) — no SPA needed. ~2–3 dev-days.

Plan impact: add "Admin console + internal key auth" to **Phase 1** (replaces the bare static-token gate), move IdP-based OAuth wholly into **Phase 2 (optional)**.

### 10.5 Read vs Write permission control (privileged writes)

Requirement: any caller may read Zendesk data; **write/edit/delete requires special permission**. Implemented as four independent layers (defense in depth):

**Layer 1 — Scoped keys (the gate).**
Every tool is classified once, centrally:

```python
TOOL_PERMISSIONS = {
    # read tools
    "get_ticket":            "tickets:read",
    "get_tickets":           "tickets:read",
    "get_ticket_comments":   "tickets:read",
    "get_ticket_attachment": "tickets:read",
    "search_tickets":        "tickets:read",
    # privileged write tools
    "create_ticket":         "tickets:write",
    "update_ticket":         "tickets:write",
    "create_ticket_comment": "tickets:write",
}
```

API keys (and OAuth tokens in Phase 2) carry scopes. **Keys are read-only by default**; `tickets:write` must be explicitly granted per key in the admin console — optionally with an expiry ("write access for 7 days"), so elevation is temporary. FastMCP middleware enforces it before dispatch:

```python
class ScopeMiddleware(Middleware):
    async def on_call_tool(self, ctx, call_next):
        required = TOOL_PERMISSIONS.get(ctx.message.name, "admin")  # unknown => deny
        if required not in get_access_token().scopes:
            raise ToolError(f"Permission denied: '{ctx.message.name}' requires scope '{required}'.")
        return await call_next(ctx)
```

Note the fail-closed default: a tool missing from the map is treated as privileged.

**Layer 2 — Tool visibility filtering (the UX).**
The same middleware hooks `on_list_tools`: a read-only caller **never even sees** write tools in `tools/list`. The model can't attempt what it can't see — fewer failed calls, no prompt-injection target ("call update_ticket…" fails at discovery).

**Layer 3 — Human-in-the-loop confirmation (the brake).**
Even with write scope, write tools are annotated (`readOnlyHint: false`, `destructiveHint` for update/delete) so clients show confirmation UI. Optionally (config `WRITE_CONFIRMATION=elicit`), write tools call MCP **elicitation** before executing — the end user gets a structured "Post this public comment to ticket #123? [approve/decline]" prompt inside their AI client. Recommended ON for `create_ticket_comment` with `public=true`.

**Layer 4 — Zendesk-side least privilege (the backstop).**
Implemented **inside the MCP server** as dual-identity routing; enforcement power comes from Zendesk's role system. Mechanics: Zendesk API tokens are account-level — the *role lives on the user email* paired with the token (`email/token:token`), not on the token itself. Setup:

1. Two Zendesk users: `mcp-reader@…` (restricted role) and `mcp-writer@…` (full agent). A **light agent** (free on most plans) can view but not edit tickets — good read identity; fully custom "view-only" roles require an Enterprise plan.
2. Server config holds both credential pairs; `ZendeskClient` builds two Zenpy clients and routes read tools → reader identity, write tools → writer identity — unconditionally, regardless of the caller's MCP key scopes (independent of Layer 1).
3. If the app-level gate is ever bypassed, a write through the reader identity gets 403 from Zendesk itself.

Degrades gracefully: without a restricted role available, both identities can point to the same user and Layer 4 becomes a no-op until the Zendesk plan allows it. Admin console note: the read-only/read-write choice when creating an MCP API key sets Layer 1 scopes only; Layer 4 routing is fixed per-tool.

**Cross-cutting:** every write attempt (allowed or denied) goes to the append-only audit log with key id, tool, arguments hash, and outcome. Admin console shows per-key write activity.

Future option if needed: an **approval queue** mode where write calls from certain keys are held as "pending" until approved in the admin console — deferred unless a real need appears; elicitation covers most cases with far less machinery.

---

## 11. Implementation Status (2026-07-24)

Phase 0 + Phase 1 core implemented and verified:

| Item | Status | Where |
|---|---|---|
| FastMCP migration (v3.4) | ✅ Done | `server.py` — `build_server()` factory, typed tool signatures, structured output |
| Streamable HTTP + stdio transports | ✅ Done | `MCP_TRANSPORT=stdio\|http`; HTTP serves `/mcp` |
| Blocking I/O fix | ✅ Done | All Zendesk tools run with `run_in_thread=True` |
| Internal API keys (Layer 1) | ✅ Done | `keystore.py` (SQLite, SHA-256 hashed, scopes, expiry, revocation, audit log) + `auth.py` (`ApiKeyVerifier`) |
| Scope enforcement + fail-closed | ✅ Done | `permissions.py` — `ScopeMiddleware.on_call_tool`, unknown tools denied |
| Tool visibility filtering (Layer 2) | ✅ Done | `ScopeMiddleware.on_list_tools` / `on_list_resources` |
| Tool annotations (Layer 3, partial) | ✅ Done | `readOnlyHint` / `destructiveHint` on all tools (elicitation deferred to Phase 3) |
| Dual Zendesk identity (Layer 4) | ✅ Done | `zendesk_client.py` — `ZENDESK_READ_EMAIL` / `ZENDESK_WRITE_EMAIL` routing |
| Key management CLI | ✅ Done | `zendesk-keys create\|list\|revoke` (admin web UI still pending) |
| `search_tickets` tool (Tier 1 bonus) | ✅ Done | Zendesk Search API via zenpy |
| `/health` endpoint | ✅ Done | Custom route, used by Docker HEALTHCHECK / ALB |
| Docker HTTP-first | ✅ Done | `Dockerfile` (EXPOSE 8000, HEALTHCHECK, `/data` volume), `docker-compose.yml` + `Caddyfile` (auto-TLS) |
| Tests | ✅ Done | 24 tests: tool parity, keystore, scope middleware; plus HTTP smoke test verified (401 unauth, filtered tools/list, denied writes, revocation) |
| CI | ✅ Done | pytest + Docker build added to GitHub Actions |

### Round 2 (same day) — remaining items implemented

| Item | Status | Where |
|---|---|---|
| Tier 1 tools | ✅ Done | `get_user`, `search_users`, `list_groups`, `list_ticket_fields`, `search_articles`, `upload_attachment` (+ comment attachments) — 14 tools total |
| Cursor pagination | ✅ Done | `get_tickets` and `get_ticket_comments` use `page[size]`/`page[after]`; offset pagination removed |
| Elicitation (Layer 3 complete) | ✅ Done | `MCP_WRITE_CONFIRMATION=true` → in-client approve/decline before PUBLIC comments; fails safe if client lacks elicitation |
| Web admin console (§10.4) | ✅ Done | `admin.py` — separate port (127.0.0.1:9000), password login + CSRF, connection hot-swap (`runtime.py` ClientHolder) with test button, key create/revoke UI, token never re-displayed, audit log |
| Terraform / EC2 (§5) | ✅ Done | `terraform/` — EC2 + SG (443/80 only, no SSH), EIP, SSM role, IMDSv2, optional Route53; `docs/DEPLOYMENT.md` runbook |
| Lockfiles | ✅ Done | `uv.lock` + `requirements.lock` regenerated (85 pinned deps) |

Verified: 26 unit tests passing; HTTP smoke test (401 unauth, scope filtering — read key sees 9 tools, `search_articles` correctly hidden without kb:read, write key sees 14, revocation immediate); admin console smoke test (login, CSRF rejection, key lifecycle via UI, token non-disclosure).

### Round 3 (same day) — Phase 2 items implemented

Decisions taken (with user approval): **generic OIDC** for OAuth (provider-agnostic, works with Auth0/Descope/Cognito/Keycloak) and **per-key tenants** for multi-tenancy.

| Item | Status | Where |
|---|---|---|
| OAuth 2.1 resource server (Phase 2) | ✅ Done | `auth.py` — `MCP_AUTH_MODE=keys\|oauth\|both`; JWKS/issuer/audience JWT validation, RFC 9728 metadata served at `/.well-known/oauth-protected-resource/mcp`, `MultiAuth` combines OAuth + internal keys |
| Multi-tenancy (§4.4 Phase 2 fork) | ✅ Done | `tenants` table (own Zendesk creds), key→tenant binding, OAuth tenant claim (`MCP_OAUTH_TENANT_CLAIM`), per-tenant client cache in `runtime.py`, tenant CRUD in admin console + CLI `--tenant-id`; deleting a tenant revokes its keys |
| ECS Fargate + ALB (§5.3) | ✅ Done | `terraform/ecs/` — ECR, cluster, Fargate service, ALB+ACM+TLS1.3, EFS-backed key store, Secrets Manager injection, CloudWatch, Route53 alias; desired_count=1 until key store moves to DynamoDB |
| MCP Registry publication | ✅ Done (prepared) | `server.json` manifest + `docs/REGISTRY.md` publishing guide (requires deployed public URL — fill placeholders, `mcp-publisher publish`) |

Verified: 38 tests passing (tenancy CRUD/routing, auth-mode factory, verifier tenant claims); OAuth smoke test over HTTP (RFC 9728 metadata 200 with correct auth servers/scopes, fake JWT → 401, zmk key works in `both` mode); admin console smoke re-passed after tenant UI additions.

Remaining truly-external items: configure a real IdP tenant (Auth0/Descope/Cognito — 30 min of console work, envs documented in `.env.example`); DynamoDB key store migration before `desired_count > 1`; fill `server.json` placeholders and run `mcp-publisher publish` once deployed.
