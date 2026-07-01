# Feature: Brain Orb — The Self-Rendering Mind (trinity-enterprise#58)

> **Type**: feature · P2 · `theme-ui-ux` · first child of the tighter-Cornelius-integration epic
>
> **One-line**: Capability-gated per-agent page that renders a Cornelius-class agent's live 3D knowledge-graph orb from data the agent produces in its own container, with live scope control and a client-held voice tile. **Shipped: Phase 1 (static render + read path) + Phase 2 (scope mount/unmount → re-export → live rebuild) + Phase 3 (client-held Gemini Live voice + read-only KB search).** KB-write actions, transcript capture, and headless-skill injection are deferred to later epic children.

## Scope (and what is deferred)

The full issue describes voice (client-held Gemini Live), a scope mount→re-export→rebuild loop, KB-write actions, automatic transcript capture, and headless-skill injection. Delivered in self-contained, default-OFF phases:

- **Phase 1 — static render + read path.** First-party CSP-clean orb assets, gated route/tab, read-only `data.json` proxy.
- **Phase 2 — live scope control.** Button-driven scope mount/unmount → agent re-export → live in-place rebuild, via owner-gated broker + agent convention hooks. No Gemini/voice.
- **Phase 3 — client-held voice tile + read-only KB search.** The browser holds its own Gemini Live socket (ephemeral-token broker); visual tools + scope-by-voice run in-browser; a read-only `search` hook backs whole-vault lookup. KB WRITES stay off.

**Deferred to epic children:** KB write actions (capture/link/run-skill) · automatic transcript-capture pipeline · headless skill injection.

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

## Phase 3 — client-held Gemini Live voice tile + read-only KB search (#60)

The orb's voice tile (`#voiceTile`, un-hidden when voice is available) holds its **own** Gemini Live session **client-side**: the browser connects DIRECTLY to `wss://generativelanguage.googleapis.com` and streams mic/playback inside the same-origin iframe. Trinity never proxies the audio. This is a deliberate design choice (distinct from Trinity's backend-proxied workspace voice, VOICE-001) — it keeps the voice→tool→orb loop entirely in the browser.

```
voice tile Start → (voice iframe) requests a token from the parent orb page
  orb.js POST /api/agents/{name}/brain-orb/voice-token   (AuthorizedAgentByName; Bearer JWT; rate-limited)
    → brain_orb_voice_service.mint_voice_token()
        v1alpha genai.Client.auth_tokens.create(CreateAuthTokenConfig(
            uses=1, new_session_expire_time≈60s, expire_time=VOICE_MAX_DURATION,
            live_connect_constraints=LiveConnectConstraints(model=VOICE_MODEL, config=<LOCKED: prompt+voice+tools>)))
    ← {ephemeral_token, model, voice_name, expires_at, tools:[…]}   (field is NOT `token`)
  orb.js relays {token,model,…} to the voice iframe over postMessage  (JWT stays in orb.js)
  voice iframe → wss://…/v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=<token>
                 setup = {model}    (config is locked in the token)

Gemini toolCall → voice iframe → postMessage 'orb-tool' → orb.js ORB_TOOLS[name]() runs in-browser
                → 'orb-tool-result' → voice iframe → tool_response back to Gemini   (no server hop)
  · visual tools (highlight/navigate/list/…)  — pure Three.js, in-browser
  · mount_scope/unmount_scope                  — reuse the Phase-2 POST /scope broker
  · navigate_to_note vault fallback            — POST /brain-orb/tool → agent ~/.trinity/brain-orb/search
```

**Why client-held is safe without proxying audio:** the security envelope is the ephemeral token's own constraints, minted server-side — model-locked, whole-config-locked (so the browser can't widen the tool surface), single new-session use, and a short expiry. Trinity's only job is the JWT gate on the mint + a per-(user,agent) rate limit. No Redis ticket/intent dance (that pattern authenticates a socket back to *Trinity's* `/ws`; here the browser talks to Google).

**The JWT relay (three documents deep):** the voice tile is a nested iframe (host `AgentBrainOrb.vue` → `/brain-orb/index.html` → `voice/orb.html`) and must never hold the platform JWT. So the **orb page** (which received the JWT via the Phase-1 postMessage handshake) mints the token and relays only the short-lived Google token down to the voice iframe. Reconnect (the token is `uses=1`) re-requests a fresh token through the same relay.

**Writes stay off by construction (KB writes are a later child):** (1) the locked tool manifest declares only read/visual/scope tools — the browser cannot add `capture_note`/`find_connections`/`synthesize_insights`; (2) there is no `/session` route, so orb.js's `initActions()` keeps `ACTIONS.enabled=false` and the write panel stays hidden; (3) the voice-token field is `ephemeral_token`, never `token` (a `token` would flip `ACTIONS.enabled` on). Regression-locked by `tests/unit/test_brain_orb.py`.

**Read-only KB search:** `POST /api/agents/{name}/brain-orb/tool` (`AuthorizedAgentByName` — read) proxies to the agent-server `POST /api/brain-orb/tool`, which runs the agent's `~/.trinity/brain-orb/search` hook (a third convention sibling next to `scopes`/`scope`) via the same hardened `_run_hook` runner. Scope-aware and read-only by hook contract (fails closed to the core scope when none is mounted); 404 when the agent ships no `search` hook (the orb degrades to in-graph search).

**CSP / assets (no nginx change):** `connect-src` already allows bare `wss:`, so the direct browser→Gemini socket needs no CSP edit. `script-src 'self'` means the Gemini client is **hand-rolled** (no vendored SDK, no CDN), the voice logic is externalized to `voice/voice.js`, and the mic AudioWorklet ships as a same-origin `voice/mic-worklet.js` (a `blob:` worklet would be blocked). The standalone Cornelius page's **hardcoded API key and p5-from-CDN visualiser are stripped**. The host iframe carries `allow="microphone"` so the nested `getUserMedia` resolves.

**Voice gating:** a new platform flag `brain_orb_voice_available` = `BRAIN_ORB_VOICE_ENABLED && GEMINI_API_KEY` (default OFF) — **distinct** from the static `brain_orb_available` (which has no Gemini dependency). The orb un-hides the voice tile only when the host passes `voiceAvailable:true` in the init handshake (platform flag) AND the agent is brain-orb-capable. The mint route is independently flag-gated (404 when off, 503 when no key), so the flag is UI-only and can't be bypassed.

**Residual /verify-only risk (documented):** the exact ephemeral-token browser handshake (`BidiGenerateContentConstrained` + `access_token=` + v1alpha) is only verifiable against the live Gemini API — mirror the SDK's `live.py` Constrained path (setup = `{model}` only, config deleted). Non-blocking function calling for the 180s scope re-export on the pinned `google-genai==1.12.1` should be confirmed in the same spike. A real agent must ship an executable `~/.trinity/brain-orb/search` hook.

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

- Platform flag: `BRAIN_ORB_ENABLED` (env, default OFF) → `brain_orb_available` in `GET /api/settings/feature-flags`. The static render has **no** Gemini dependency, so unlike voice/workspace/voip it is the bare env flag.
- Voice flag (Phase 3): `BRAIN_ORB_VOICE_ENABLED && GEMINI_API_KEY` → `brain_orb_voice_available` (default OFF) — **distinct** from `brain_orb_available` because the voice tile needs a Gemini key. Un-hides the voice tile only when on AND the agent is brain-orb-capable; the `voice-token` mint route is independently flag-gated.
- Per-agent capability: a **generalizable** `brain-orb` token in the agent's `template.yaml capabilities` list (surfaced by `GET /api/agents/{name}/info`, read frontend-side) — never a hardcoded agent/template name. Mirrors the `sessionAvailable` + `hasDashboard` idioms.
- The **route guard** checks only the platform flag (workspace precedent); the **tab** checks both. A deep link to a non-capable agent loads but the proxy 404s → empty state.

## Auth

The data route uses standard `AuthorizedAgentByName` Bearer auth like every other `/api/agents/{name}/*` route — no new ticket primitive. A `fetch()` from the same-origin iframe doesn't auto-carry the JWT, so the host hands it over via origin-pinned `postMessage` (`targetOrigin = window.location.origin`); the token never enters a URL. The agent-server route is auto-gated by the #1159 `X-Trinity-Agent-Token` middleware (only `/health` is exempt).

## Files

| Layer | Path |
|-------|------|
| Orb assets | `src/frontend/public/brain-orb/{index.html, orb.js, styles.css, orb-trinity.css, vendor/*}` |
| Voice tile (Phase 3) | `src/frontend/public/brain-orb/voice/{orb.html, voice.js, mic-worklet.js}` |
| Frontend host | `src/frontend/src/views/AgentBrainOrb.vue` (`allow="microphone"`, relays `voiceAvailable`) |
| Route | `src/frontend/src/router/index.js` (`/agents/:name/brain`) |
| Flag (FE) | `src/frontend/src/stores/sessions.js` (`brainOrbAvailable`, `brainOrbVoiceAvailable`) |
| Tab + capability | `src/frontend/src/views/AgentDetail.vue` (`visibleTabs`, `checkBrainOrbCapability`) |
| Backend proxy | `src/backend/routers/agent_brain_orb.py` (data/scopes/scope + **voice-token/tool**) |
| Mint service (Phase 3) | `src/backend/services/brain_orb_voice_service.py` (v1alpha ephemeral token + locked tool manifest) |
| Flag (BE) | `src/backend/config.py` (`BRAIN_ORB_ENABLED`, `BRAIN_ORB_VOICE_ENABLED`), `src/backend/routers/settings.py` |
| Agent-server | `docker/base-image/agent_server/routers/brain_orb.py` (data/scopes/scope + **search** hook) |
| Tests | `tests/unit/test_brain_orb.py` |

## Invariants honored

#5 agent-server mirror · #8 agent owns generation (Trinity only reads) · #4 route order (the 5-segment path never collides with the `/{name}` catch-all) · #15 agent-scoped nesting. No MCP tool (this is a UI page, not an agent-facing tool), no DB change, no migration, no new secret.

## Known limitations / follow-ups

- `data.json` is multi-MB and re-fetched per visit (`Cache-Control: no-store`); a future refresh/cache strategy can ride the same proxy.
- `/brain-orb/orb.js` is a non-hashed asset under nginx's 1y-immutable static cache — a future orb update needs a cache-bust (query param / rename). The orb is frozen (verbatim) for now.
- Visual + functional parity (AC) is verified at the asset level (vendored bundle renders the real `data.json`); full in-stack parity needs a real Cornelius agent.
- Scope `POST /scope` is owner-gated (which bounds abuse) but not yet rate-limited; a per-agent rate limit on the re-export trigger is a cheap hardening follow-up. The agent-side `scope` hook is responsible for serializing concurrent re-exports (the Cornelius proxy uses an RLock).
