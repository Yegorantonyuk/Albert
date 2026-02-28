# multiagent/

Multi-agent system: run multiple independent ductor agents in a single process with inter-agent communication.

## Files

- `supervisor.py`: `AgentSupervisor` lifecycle, crash recovery, `agents.json` watcher, sub-agent start/stop
- `stack.py`: `AgentStack` container (workspace + TelegramBot + config per agent)
- `bus.py`: `InterAgentBus` in-memory message passing (sync + async)
- `internal_api.py`: `InternalAgentAPI` localhost HTTP bridge for CLI tool scripts
- `models.py`: `SubAgentConfig`, `merge_sub_agent_config`
- `registry.py`: `AgentRegistry` read/write access to `agents.json`
- `health.py`: `AgentHealth` runtime health tracking per agent
- `shared_knowledge.py`: `SharedKnowledgeSync` watcher for `SHAREDMEMORY.md`
- `commands.py`: Telegram commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`)

## Purpose

Runs multiple ductor agents inside one process. Each agent has its own Telegram bot, workspace, sessions, and CLI service. Agents communicate via an in-memory bus and share knowledge through a synchronized file.

Key differences from `/session` (named background sessions):

- `/session` runs tasks in the **same agent** without blocking the chat.
- Multi-agent runs **separate agents** with their own Telegram bots, workspaces, and provider configurations.
- Agents communicate via tool scripts; background sessions share the parent agent's CLI service.

## Architecture

```text
AgentSupervisor
  |
  +-- AgentStack "main"
  |     TelegramBot -> Orchestrator -> CLIService -> provider
  |
  +-- AgentStack "sub-agent-1"
  |     TelegramBot -> Orchestrator -> CLIService -> provider
  |
  +-- AgentStack "sub-agent-2"
  |     TelegramBot -> Orchestrator -> CLIService -> provider
  |
  +-- InterAgentBus (in-memory)
  |     sync send() + async send_async()
  |
  +-- InternalAgentAPI (127.0.0.1:8799)
  |     /interagent/send, /interagent/send_async, /interagent/agents, /interagent/health
  |
  +-- SharedKnowledgeSync
  |     SHAREDMEMORY.md -> all agents' MAINMEMORY.md
  |
  +-- FileWatcher (agents.json)
        auto-detect add/remove/token-change
```

Each `AgentStack` is a complete bot pipeline: its own `TelegramBot` -> `Orchestrator` -> `CLIService` -> provider subprocess. Stacks are isolated — no shared session state, no shared CLI processes.

## Setup

### Creating sub-agents

Each sub-agent needs its own Telegram bot token (from @BotFather).

**CLI method** (interactive):

```bash
ductor agents add myagent
```

Prompts for token, user IDs, provider, and model. Writes to `~/.ductor/agents.json`.

**Tool script** (from within a CLI session):

```bash
python3 tools/agent_tools/create_agent.py \
  --name "myagent" \
  --token "BOT_TOKEN" \
  --users "USER_ID1,USER_ID2" \
  --provider claude \
  --model sonnet
```

**Manual**: edit `~/.ductor/agents.json` directly. The supervisor watches this file and starts new agents automatically.

### `agents.json` format

```json
[
  {
    "name": "myagent",
    "telegram_token": "123456:ABC...",
    "allowed_user_ids": [12345678],
    "provider": "claude",
    "model": "sonnet"
  }
]
```

Top-level JSON array. Each entry is a `SubAgentConfig`.

### Listing and removing

```bash
ductor agents list          # show all sub-agents
ductor agents remove myagent  # remove from registry
```

Removing a sub-agent stops its Telegram bot but preserves its workspace under `~/.ductor/agents/<name>/`.

## Configuration

### `SubAgentConfig` fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `str` | yes | Unique lowercase identifier |
| `telegram_token` | `str` | yes | Separate bot token from @BotFather |
| `allowed_user_ids` | `list[int]` | no | Defaults to empty list |
| `provider` | `str` | no | Inherits from main if omitted |
| `model` | `str` | no | Inherits from main if omitted |
| `log_level` | `str` | no | Inherits from main |
| `idle_timeout_minutes` | `int` | no | Inherits from main |
| `session_age_warning_hours` | `int` | no | Inherits from main |
| `daily_reset_hour` | `int` | no | Inherits from main |
| `daily_reset_enabled` | `bool` | no | Inherits from main |
| `max_budget_usd` | `float` | no | Inherits from main |
| `max_turns` | `int` | no | Inherits from main |
| `max_session_messages` | `int` | no | Inherits from main |
| `permission_mode` | `str` | no | Inherits from main |
| `cli_timeout` | `float` | no | Inherits from main |
| `reasoning_effort` | `str` | no | Inherits from main |
| `file_access` | `str` | no | Inherits from main |
| `streaming` | `StreamingConfig` | no | Inherits from main |
| `docker` | `DockerConfig` | no | Inherits from main |
| `heartbeat` | `HeartbeatConfig` | no | Inherits from main |
| `cleanup` | `CleanupConfig` | no | Inherits from main |
| `webhooks` | `WebhookConfig` | no | Inherits from main |
| `api` | `ApiConfig` | no | Disabled by default for sub-agents |
| `cli_parameters` | `CLIParametersConfig` | no | Inherits from main |
| `user_timezone` | `str` | no | Inherits from main |

### Config merge behavior

`merge_sub_agent_config()` creates a full `AgentConfig` for each sub-agent:

1. Start with the main agent's config as base.
2. Override with any non-None fields from `SubAgentConfig`.
3. Always set `ductor_home` to `~/.ductor/agents/<name>/`.
4. Always use `telegram_token` and `allowed_user_ids` from the sub-agent definition.
5. Disable `api.enabled` unless the sub-agent explicitly provides an `api` config (sub-agents use the InterAgentBus, not the user-facing API server).

### What is isolated per sub-agent

- Telegram bot (own token, own allowlist)
- Workspace (`~/.ductor/agents/<name>/workspace/`)
- Sessions (`~/.ductor/agents/<name>/sessions.json`)
- Cron jobs, webhooks, heartbeat, cleanup
- `MAINMEMORY.md` (plus shared knowledge injection)

### What is shared

- Python process and event loop
- InterAgentBus (in-memory)
- `SHAREDMEMORY.md` (synced into all agents)
- central rotating log file at `~/.ductor/logs/agent.log` (all agents write there with agent context)
- Docker container (when enabled on main agent and inherited)

## Inter-agent communication

### InterAgentBus

In-memory message bus. All agents in the same process register on it. Messages are handled by calling the target agent's `Orchestrator.handle_interagent_message()`, which runs a one-shot CLI turn.

### Tool scripts

CLI subprocesses cannot access the in-memory bus directly. Tool scripts use the `InternalAgentAPI` (localhost HTTP on port 8799) as a bridge.

Environment variables set by the framework:

| Variable | Description |
|---|---|
| `DUCTOR_AGENT_NAME` | Current agent's name |
| `DUCTOR_INTERAGENT_PORT` | Internal API port (default `8799`) |
| `DUCTOR_INTERAGENT_HOST` | Internal API host (`host.docker.internal` in Docker exec, otherwise tool scripts default to `127.0.0.1`) |

#### Synchronous (`ask_agent.py`)

Blocks until the target agent responds. Use for quick lookups.

```bash
python3 tools/agent_tools/ask_agent.py TARGET_AGENT "Your question"
```

The sender's CLI turn blocks for up to 5 minutes (bus timeout).

Execution note:

- synchronous inter-agent turns run through `Orchestrator.handle_interagent_message()` with synthetic `chat_id=0` (not user-chat scoped).

#### Asynchronous (`ask_agent_async.py`)

Returns immediately with a `task_id`. The response is delivered to the sender agent's primary Telegram user (`allowed_user_ids[0]`) when ready.

```bash
python3 tools/agent_tools/ask_agent_async.py TARGET_AGENT "Complex task"
```

Use async for anything that may take more than a few seconds.

#### Shared knowledge (`edit_shared_knowledge.py`)

View or edit `SHAREDMEMORY.md`, which is synced into every agent's `MAINMEMORY.md`.

```bash
python3 tools/agent_tools/edit_shared_knowledge.py --show
python3 tools/agent_tools/edit_shared_knowledge.py --append "New fact"
python3 tools/agent_tools/edit_shared_knowledge.py --set "Full replacement"
```

#### Other tool scripts

| Tool | Purpose | Availability |
|---|---|---|
| `create_agent.py` | Create a new sub-agent (writes to `agents.json`) | Main only |
| `remove_agent.py` | Remove a sub-agent from registry | Main only |
| `list_agents.py` | List all sub-agents and their config | Main only |

## SharedKnowledgeSync

`SharedKnowledgeSync` watches `~/.ductor/SHAREDMEMORY.md` via `FileWatcher`.

On change, it injects the content into every agent's `MAINMEMORY.md` between markers:

```text
--- SHARED KNOWLEDGE START ---
(content from SHAREDMEMORY.md)
--- SHARED KNOWLEDGE END ---
```

If no markers exist in `MAINMEMORY.md`, the block is appended. Legacy HTML markers (`<!-- SHARED:START/END -->`) are detected on read and migrated to the new format on write.

A seed `SHAREDMEMORY.md` is created on first supervisor start if the file does not exist.

## Telegram commands

Available on the main agent only (registered when supervisor is present):

| Command | Description |
|---|---|
| `/agents` | List all agents with health status |
| `/agent_start <name>` | Start a sub-agent from the registry |
| `/agent_stop <name>` | Stop a running sub-agent |
| `/agent_restart <name>` | Restart a sub-agent (stop + start) |

`/agents` shows status indicators: `●` running, `◐` starting, `✖` crashed, `○` stopped. Includes uptime and restart count.

`/status` and `/diagnose` on the main agent include multi-agent health sections when a supervisor is active.

## CLI commands

| Command | Description |
|---|---|
| `ductor agents` | List all sub-agents and their config |
| `ductor agents list` | Same as above |
| `ductor agents add <name>` | Add a new sub-agent (interactive prompt) |
| `ductor agents remove <name>` | Remove a sub-agent from the registry |

When the bot is running, `ductor agents list` queries live health from the internal API (`/interagent/health`).

## Health monitoring

`AgentHealth` tracks per-agent state:

| Status | Description |
|---|---|
| `running` | Agent is actively polling Telegram |
| `starting` | Agent is initializing or restarting |
| `crashed` | Agent threw an unhandled exception |
| `stopped` | Agent is not running |

### Crash recovery

`_supervised_run()` wraps each agent with automatic crash recovery:

- On crash: exponential backoff retry (5s, 10s, 20s, 40s, 80s), max 5 retries.
- After max retries: agent is stopped and the main agent is notified via Telegram.
- On restart request (exit code 42):
  - Main agent: full service restart (propagated to supervisor).
  - Sub-agent: in-process hot-reload (rebuild stack only, no service restart).
- Main agent crash: supervisor terminates immediately.

## Docker mode

When `docker.enabled` is true on the main agent's config and inherited by a sub-agent, both share the same Docker container. The workspace is initialized inside the container for each agent independently.

## File layout

```text
~/.ductor/
  agents.json                   # sub-agent registry
  SHAREDMEMORY.md               # shared knowledge (synced to all agents)
  agents/
    myagent/
      config/config.json        # (inherited, auto-generated)
      workspace/                # agent workspace
      sessions.json             # agent sessions
      cron_jobs.json
      webhooks.json
    another-agent/
      ...
```

Sub-agent homes do not create a dedicated `logs/` directory by default.
Logging stays centralized in the main home at `~/.ductor/logs/agent.log`.

## InternalAgentAPI

Localhost-only HTTP server on `127.0.0.1:8799`. Bridges CLI subprocesses to the InterAgentBus.

Startup behavior:

- startup failure (for example port already bound) is logged but does not stop supervisor startup; agents still run without HTTP bridge endpoints.

| Endpoint | Method | Description |
|---|---|---|
| `/interagent/send` | POST | Synchronous message, blocks until response |
| `/interagent/send_async` | POST | Async message, returns `task_id` immediately |
| `/interagent/agents` | GET | List registered agent names |
| `/interagent/health` | GET | Live health for all agents |

### POST `/interagent/send`

```json
{"from": "agent_name", "to": "target_agent", "message": "..."}
```

Response: `{"sender": "...", "text": "...", "success": true, "error": null}`

### POST `/interagent/send_async`

Same request body. Response: `{"success": true, "task_id": "abc123"}`

The result is delivered to the sender agent's primary Telegram user (`allowed_user_ids[0]`) when the target finishes processing.

## Wiring

### Supervisor injection

`AgentSupervisor._inject_supervisor_hook()` registers a dispatcher startup handler on each `AgentStack`. After `TelegramBot._on_startup()` completes and the orchestrator is available, the hook:

1. Sets `orch._supervisor` reference.
2. On the main agent: registers multi-agent commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`).

### agents.json watcher

`FileWatcher` polls `agents.json` every 5 seconds. On mtime change:

- New entries: start sub-agent.
- Removed entries: stop sub-agent.
- Token changes on existing entries: restart sub-agent.
- Other config field changes currently do not trigger auto-restart.
