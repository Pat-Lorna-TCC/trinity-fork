# Feature: Brain Orb — The Self-Rendering Mind (trinity-enterprise#58)

> **Type**: feature · P2 · `theme-ui-ux` · first child of the tighter-Cornelius-integration epic
>
> **One-line**: Capability-gated per-agent page that renders a Cornelius-class agent's live 3D knowledge-graph orb from data the agent produces in its own container, with live scope control. **Shipped: Phase 1 (static render + read path) + Phase 2 (scope mount/unmount → re-export → live rebuild).** Voice, KB-write actions, transcript capture, and headless-skill injection are deferred to later epic children.

## Scope (and what is deferred)

The full issue describes voice (client-held Gemini Live), a scope mount→re-export→rebuild loop, KB-write actions, automatic transcript capture, and headless-skill injection. Delivered in self-contained, default-OFF phases:

- **Phase 1 — static render + read path.** First-party CSP-clean orb assets, gated route/tab, read-only `data.json` proxy.
- **Phase 2 — live scope control.** Button-driven scope mount/unmount → agent re-export → live in-place rebuild, via owner-gated broker + agent convention hooks. No Gemini/voice.

**Deferred to epic children:** Gemini Live voice tile · KB write actions · automatic transcript-capture pipeline · headless skill injection.

## Phase 2 — live scope control

The orb's scope panel (the `S` key, un-hidden in Phase 2) lists the agent's vault scopes; toggling one mounts/unmounts it, the agent re-exports at the new scope (rewriting `data.json`), and the orb re-fetches `/data` and rebuilds **in place** (no reload). The old localhost voice proxy's per-start `X-Orb-Token` is replaced by the platform JWT (carried on the brokered fetches) + an owner gate on the mutation.

```
orb scope toggle → setScope(tokens)
  POST /api/agents/{name}/brain-orb/scope   (OwnedAgentByName; Bearer; body {tokens|mount|unmount})
    → agent-server POST /api/brain-orb/scope
        → runs ~/.trinity/brain-orb/scope  (stdin = body)  → mutate active set + re-export → rewrite data.json
        → stdout JSON {ok, active, nodes, edges}
  ← orb re-fetches GET .../brain-orb/data → applyData() rebuilds the graph in place

orb panel open → loadScopes()
  GET /api/agents/{name}/brain-orb/scopes   (AuthorizedAgentByName; Bearer)
    → agent-server GET /api/brain-orb/scopes → runs ~/.trinity/brain-orb/scopes → {active, available}
```

**Agent convention (Invariant #8 — agent owns scope state + generation):** the agent ships two executable hooks under `~/.trinity/brain-orb/` (mirrors `~/.trinity/pre-check`, #454) — `scopes` (prints `{active, available}` JSON) and `scope` (reads a `{tokens|mount|unmount}` JSON request on stdin, mutates the active set, re-runs its exporter, prints `{ok, active, nodes, edges}`). Trinity stays generic: the agent-server runs the hooks via **hardened async subprocess** (timeout-kill, 4 MB output cap, JSON-parse + non-zero-exit guards), and **404s when a hook is absent** (the agent doesn't support scope control → the orb's scope panel shows a degraded hint). The real Cornelius agent adopts the convention in its own repo — this PR ships the generic surface (like `pre-check`, `data_paths`, pipelines).

**Auth split:** `GET /scopes` is `AuthorizedAgentByName` (read); **`POST /scope` is the only mutating brain-orb route and is `OwnedAgentByName`** (owner/admin) — body-capped at 64 KB, with a 200s backend timeout sitting just above the agent hook's 180s so a slow re-export surfaces as the agent's 504, not a premature one.

## Why first-party + iframe (the CSP nuance, #979)

The orb is a vanilla Three.js page with an inline ES-module, CDN deps, and a `localhost:8770` voice proxy. Prod CSP is `script-src 'self'; font-src 'self'; frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN`. #979 only bit because it iframed **agent-origin** content with **inline** scripts. The resolution:

- Ship the orb as **first-party static assets** under `src/frontend/public/brain-orb/` (served from the frontend origin) → same-origin, external scripts → CSP-clean with **no nginx change**.
- Host it in a thin Vue view via a **same-origin iframe** — first-party, not agent-origin, so it does not trip #979.

Mechanical edits only (AC #1): externalize the inline module to `orb.js`; vendor `three`/`marked`/`DOMPurify`/JetBrains-Mono locally (drop the importmap + Google-Fonts link); repoint the data fetch at the backend proxy; neutralize the deferred voice-proxy base (`VOICE_PROXY=''`); hide the voice/scope/action panels via `orb-trinity.css`. DOMPurify sanitizes rendered note bodies (H-005).

## End-to-end flow

```
AgentDetail.vue  (visibleTabs: Brain tab when brainOrbAvailable && capabilities⊇'brain-orb')
   │ select tab → router.push
   ▼
/agents/:name/brain  (router beforeEnter: redirect unless sessionsStore.brainOrbAvailable)
   ▼
AgentBrainOrb.vue  ── same-origin iframe ──>  /brain-orb/index.html  (first-party static page)
   │  postMessage handshake (origin-pinned):
   │    iframe → host:  {type:'brain-orb:ready'}
   │    host  → iframe: {type:'brain-orb:init', agentName, apiBase:'', authToken: <JWT>}
   │    iframe → host:  {type:'brain-orb:error'}  → host shows "hasn't rendered its mind yet"
   ▼ orb.js loadData()
GET /api/agents/{name}/brain-orb/data   (Authorization: Bearer <JWT>)
   ▼ routers/agent_brain_orb.py  — AuthorizedAgentByName (owner/shared); flag-gated
     agent_httpx_client(name) (#1159 per-agent token)  →  byte pass-through (no re-serialize)
   ▼
GET http://agent-{name}:8000/api/brain-orb/data   (X-Trinity-Agent-Token; auto-gated by #1159 middleware)
   ▼ agent_server/routers/brain_orb.py
FileResponse(~/resources/agent-visualization/data.json)   (agent owns generation — Invariant #8)
```

## Gating

`brainOrbAvailable = brain_orb_available (platform flag) && template.yaml.capabilities ⊇ 'brain-orb' (per-agent)`.

- Platform flag: `BRAIN_ORB_ENABLED` (env, default OFF) → `brain_orb_available` in `GET /api/settings/feature-flags`. The static render has **no** Gemini dependency, so unlike voice/workspace/voip it is the bare env flag; the deferred voice child adds its own gate.
- Per-agent capability: a **generalizable** `brain-orb` token in the agent's `template.yaml capabilities` list (surfaced by `GET /api/agents/{name}/info`, read frontend-side) — never a hardcoded agent/template name. Mirrors the `sessionAvailable` + `hasDashboard` idioms.
- The **route guard** checks only the platform flag (workspace precedent); the **tab** checks both. A deep link to a non-capable agent loads but the proxy 404s → empty state.

## Auth

The data route uses standard `AuthorizedAgentByName` Bearer auth like every other `/api/agents/{name}/*` route — no new ticket primitive. A `fetch()` from the same-origin iframe doesn't auto-carry the JWT, so the host hands it over via origin-pinned `postMessage` (`targetOrigin = window.location.origin`); the token never enters a URL. The agent-server route is auto-gated by the #1159 `X-Trinity-Agent-Token` middleware (only `/health` is exempt).

## Files

| Layer | Path |
|-------|------|
| Orb assets | `src/frontend/public/brain-orb/{index.html, orb.js, styles.css, orb-trinity.css, vendor/*}` |
| Frontend host | `src/frontend/src/views/AgentBrainOrb.vue` |
| Route | `src/frontend/src/router/index.js` (`/agents/:name/brain`) |
| Flag (FE) | `src/frontend/src/stores/sessions.js` (`brainOrbAvailable`) |
| Tab + capability | `src/frontend/src/views/AgentDetail.vue` (`visibleTabs`, `checkBrainOrbCapability`) |
| Backend proxy | `src/backend/routers/agent_brain_orb.py` |
| Flag (BE) | `src/backend/config.py` (`BRAIN_ORB_ENABLED`), `src/backend/routers/settings.py` |
| Agent-server | `docker/base-image/agent_server/routers/brain_orb.py` |
| Tests | `tests/unit/test_brain_orb.py` |

## Invariants honored

#5 agent-server mirror · #8 agent owns generation (Trinity only reads) · #4 route order (the 5-segment path never collides with the `/{name}` catch-all) · #15 agent-scoped nesting. No MCP tool (this is a UI page, not an agent-facing tool), no DB change, no migration, no new secret.

## Known limitations / follow-ups

- `data.json` is multi-MB and re-fetched per visit (`Cache-Control: no-store`); a future refresh/cache strategy can ride the same proxy.
- `/brain-orb/orb.js` is a non-hashed asset under nginx's 1y-immutable static cache — a future orb update needs a cache-bust (query param / rename). The orb is frozen (verbatim) for now.
- Visual + functional parity (AC) is verified at the asset level (vendored bundle renders the real `data.json`); full in-stack parity needs a real Cornelius agent.
- Scope `POST /scope` is owner-gated (which bounds abuse) but not yet rate-limited; a per-agent rate limit on the re-export trigger is a cheap hardening follow-up. The agent-side `scope` hook is responsible for serializing concurrent re-exports (the Cornelius proxy uses an RLock).
