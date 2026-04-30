# Slack Integration

Connect agents to Slack workspaces. Supports DMs, @mentions in channels, multi-agent routing, and thread continuity.

## Concepts

- **Channel Adapter** -- Pluggable abstraction for external messaging platforms. Slack is the first implementation; Telegram and Discord are planned.
- **Socket Mode** -- Default transport using a WebSocket connection. No public URL required. Configured via a Slack App Token (`xapp-...`).
- **Webhook Mode** -- HTTP webhook transport for production environments (fallback option).
- **Multi-Agent Routing** -- Multiple agents can share one Slack workspace. Each agent is bound to a dedicated channel. DMs are routed to a default agent; @mentions in channels route to the bound agent.
- **Thread Tracking** -- The bot automatically responds to thread replies without requiring an @mention.

## How It Works

### Platform Setup (Admin -- Settings Page)

1. Go to **Settings > Slack** section.
2. Enter your Slack App Token (`xapp-...`) for Socket Mode.
3. Click **Connect** to start the Socket Mode transport.
4. Click **Install to Workspace** to perform platform-level OAuth and obtain a bot token.
5. The connection status badge shows connected or disconnected.

### Per-Agent Channel Binding (Agent Sharing Tab)

1. Open the agent detail page and select the **Sharing** tab.
2. In the Slack Channel section, click **Create Channel**.
3. A dedicated Slack channel is created and bound to this agent.
4. All messages in that channel are routed to the bound agent.
5. To disconnect, click **Unbind**.

### Changing the DM-Default Agent

DMs to the Slack bot are routed to the workspace's **DM-default agent**. By default the first agent bound to a workspace becomes the DM default, but you can reassign it at any time.

1. Open the agent detail page for the agent you want to receive DMs.
2. Go to the **Sharing** tab → Slack Channel section.
3. Click **Set as DM Default**.
4. The previous DM-default agent retains its channel binding but no longer receives DMs.

**Rules:**
- Only one agent per workspace can be the DM default at a time.
- You cannot unbind the current DM-default agent while other agents are still bound to the workspace. Reassign the DM default to another agent first, then unbind.
- Changing the DM default takes effect immediately — no restart required.

### Message Flow

```
Transport -> Adapter -> Router -> Agent -> Response
```

| Message Type | Routing |
|--------------|---------|
| DM to bot | Default agent |
| @mention in channel | Bound agent for that channel |
| Thread reply (no @mention) | Same agent that was originally mentioned |

### Rate Limiting

| Setting | Default |
|---------|---------|
| Messages per window per Slack user | 30 |
| Window duration | 60 seconds |
| Execution timeout | 120 seconds |
| Allowed tools | WebSearch, WebFetch |

Rate limit and timeout values are configurable via settings (`channel_rate_limit_max`, `channel_rate_limit_window`).

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings/slack/status` | GET | Connection state |
| `/api/settings/slack/connect` | POST | Start Socket Mode |
| `/api/settings/slack/disconnect` | POST | Stop transport |
| `/api/settings/slack/install` | POST | OAuth install |
| `/api/agents/{name}/slack/channel` | GET | Channel binding status |
| `/api/agents/{name}/slack/channel` | POST | Create and bind channel |
| `/api/agents/{name}/slack/channel` | DELETE | Unbind channel |
| `/api/agents/{name}/slack/channel/dm-default` | PUT | Set this agent as the DM default for its workspace |

## Limitations

- Only one Slack workspace can be connected per Trinity instance.
- Webhook Mode requires a publicly accessible URL.
- Thread tracking applies only to threads started by a bot message or @mention.
- Rate limits are per Slack user, not per agent.

## See Also

- [Channel Adapters](../../memory/feature-flows/slack-channel-adapter.md)
- [Agent Sharing](../agents/sharing.md)
