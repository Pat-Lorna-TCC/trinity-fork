# Voice Chat

Real-time voice conversations with agents via Gemini 2.5 Flash Native Audio model (~280ms latency). Audio streams bidirectionally through a backend WebSocket proxy. Gemini handles speech-to-speech; Claude Code remains the agent's reasoning engine and is invoked on demand via tool calling.

## Concepts

- **Voice Session** — A live audio session bridged between the browser, Trinity backend, and Gemini Live API. Transcripts are saved to the agent's chat session on close.
- **Animated Orb** — Canvas-rendered visualization that reflects session state via color and particle movement.
- **Tool Calling (`run_task`)** — During a voice session, Gemini can delegate complex tasks to the underlying Claude agent. The orb shows an amber badge while the task runs.
- **Voice System Prompt** — Controls Gemini's persona for the session. Looked up in order: DB setting → `voice-agent-system-prompt.md` in the container → auto-generated from template info → generic fallback.

## How It Works

1. Open an agent's **Chat** tab.
2. Click the microphone button next to the chat input.
3. A full-screen voice overlay appears with an animated canvas orb.
4. Speak — audio is captured as PCM 16 kHz and streamed to the backend WebSocket.
5. The backend proxies audio to the Gemini Live API in real-time.
6. Agent response audio (PCM 24 kHz) plays back immediately (~280ms TTFT).
7. When Gemini needs to perform a complex task, it calls `run_task`:
   - The orb shifts to an **amber badge** state.
   - Trinity sends the prompt to the Claude agent (up to 30 seconds).
   - Gemini speaks the result when done; the orb returns to listening state.
8. Click **End** to close the session. Transcripts are saved to the current chat session.

### Orb State Reference

| State | Orb color | Trigger |
|---|---|---|
| Idle / Connecting | Base hue (0°) | Before audio starts |
| Listening | +90° shift (green) | Microphone active, user speaking |
| Speaking | +210° shift (indigo) | Gemini responding |
| Tool calling | Amber badge overlay | `run_task` dispatched to Claude |

### Muting

Click **Mute** to silence your microphone mid-session. Gemini continues speaking. Click again to unmute.

## Requirements

- `GEMINI_API_KEY` configured in **Settings → AI Keys**.
- `VOICE_ENABLED` must be on (default: on when API key is present).
- Browser microphone permission granted.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | API key for Gemini Live API | — (required) |
| `VOICE_ENABLED` | Global toggle | `true` |
| `VOICE_MODEL` | Gemini model ID | `gemini-2.5-flash-native-audio-preview-12-2025` |
| `VOICE_MAX_DURATION` | Max session duration in seconds | `300` |

### Per-Agent Voice Prompt

Set a custom voice system prompt for an agent by placing a file named `voice-agent-system-prompt.md` in the agent's workspace (`/home/developer/`). This controls Gemini's persona — tone, focus, and response style — independently of the agent's main `CLAUDE.md`.

If no file is present, Trinity auto-generates a prompt from the agent's template info and falls back to a generic prompt.

## Tool Calling

When Gemini encounters a request that requires complex reasoning, file access, or external actions, it calls the `run_task` function:

1. Gemini formulates a task prompt (max 2000 characters).
2. Trinity dispatches the prompt to the Claude agent via the existing chat/task path.
3. The agent runs with full tool access (read/write files, web search, MCP tools, etc.).
4. The result is returned to Gemini, which incorporates it into its spoken response.
5. If the agent is unreachable or the task times out (30s), Gemini recovers gracefully.

All `run_task` invocations are written to the platform audit log.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice/start` | POST | Start a voice session; returns `voice_session_id` and WebSocket URL |
| `/api/agents/{name}/voice/stop` | POST | End session; returns transcript and cost |
| `/api/agents/{name}/voice/status` | GET | Get current session state |
| `/ws/voice/{session_id}` | WebSocket | Bidirectional audio bridge |

### WebSocket Message Types

**Client → Server:**
```json
{ "type": "audio", "data": "<base64 PCM 16kHz audio>" }
```

**Server → Client:**
```json
{ "type": "audio",      "data": "<base64 PCM 24kHz audio>" }
{ "type": "transcript", "role": "user|assistant", "text": "..." }
{ "type": "status",     "state": "listening|speaking|processing" }
{ "type": "tool_call",  "tool_name": "run_task" }
{ "type": "tool_result","tool_name": "run_task", "result": "..." }
```

## Limitations

- Voice is available only in authenticated chat (not public links).
- One voice session per agent at a time.
- Maximum session duration: 300 seconds (configurable).
- `run_task` tool calls time out after 30 seconds.
- Incremental transcript display during the session is not yet implemented — transcripts appear in the chat after the session ends.

## See Also

- [Agent Chat](../agents/agent-chat.md)
- Backend API Docs: http://localhost:8000/docs
