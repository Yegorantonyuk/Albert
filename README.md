<p align="center">
  <strong>Albert — Claude Code, Codex CLI, and Gemini CLI as your coding assistant — on Telegram and Discord.</strong><br>
  Uses only official CLIs. Nothing spoofed, nothing proxied. Matrix and more via plugin system.
</p>

<p align="center">
  <a href="https://github.com/Yegorantonyuk/Albert/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Yegorantonyuk/Albert" alt="License" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot;
  <a href="#how-chats-work">How chats work</a> &middot;
  <a href="#commands">Commands</a> &middot;
  <a href="docs/README.md">Docs</a>
</p>

---

Albert runs on your machine and sends simple console commands as if you were typing them yourself, so you can use your active subscriptions (Claude Max, etc.) directly. No API proxying, no SDK patching, no spoofed headers. Just the official CLIs, executed as subprocesses, with all state kept in plain JSON and Markdown under `~/.albert/`.

## Quick start

```bash
git clone https://github.com/Yegorantonyuk/Albert.git
cd Albert
pipx install .
albert
```

The onboarding wizard handles CLI checks, transport setup (Telegram, Discord, or Matrix), timezone, optional Docker, and optional background service install.

**Requirements:** Python 3.11+, at least one CLI installed (`claude`, `codex`, or `gemini`), and either:

- a Telegram Bot Token from [@BotFather](https://t.me/BotFather), or
- a Discord Bot Token, or
- a Matrix account on a homeserver

Detailed setup: [`docs/installation.md`](docs/installation.md)

## How chats work

Albert gives you multiple ways to interact with your coding agents. Each level builds on the previous one.

### 1. Single chat (your main agent)

This is where everyone starts. You get a private 1:1 chat with your bot. Every message goes to the CLI you have active (`claude`, `codex`, or `gemini`), responses stream back in real time.

```text
You:   "Explain the auth flow in this codebase"
Bot:   [streams response from Claude Code]

You:   /model
Bot:   [interactive model/provider picker]

You:   "Now refactor the parser"
Bot:   [streams response, same session context]
```

This single chat is all you need. Everything else below is optional.

### 2. Groups with topics (multiple isolated chats)

**Telegram:** Create a group, enable topics (forum mode), and add your bot.
**Matrix:** Invite the bot to multiple rooms — each room is its own context.

Every topic (Telegram) or room (Matrix) becomes an isolated chat with its own CLI context.

```text
Group: "My Projects"
  ├── General           ← own context (isolated from your single chat)
  ├── Topic: Auth       ← own context
  ├── Topic: Frontend   ← own context
  ├── Topic: Database   ← own context
  └── Topic: Refactor   ← own context
```

That's 5 independent conversations from a single group. Your private single chat stays separate too — 6 total contexts, all running in parallel.

Each topic can use a different model. Run `/model` inside a topic to change just that topic's provider.

All chats share the same `~/.albert/` workspace — same tools, same memory, same files. The only thing isolated is the conversation context.

### 3. Named sessions (extra contexts within any chat)

Need to work on something unrelated without losing your current context? Start a named session. It runs inside the same chat but has its own CLI conversation.

```text
You:   "Let's work on authentication"        ← main context builds up
Bot:   [responds about auth]

/session Fix the broken CSV export            ← starts session "firmowl"
Bot:   [works on CSV in separate context]

You:   "Back to auth — add rate limiting"     ← main context is still clean
Bot:   [remembers exactly where you left off]

@firmowl Also add error handling              ← follow-up to the session
```

Sessions work everywhere — in your single chat, in group topics, in sub-agent chats.

### 4. Background tasks (async delegation)

Any chat can delegate long-running work to a background task. You keep chatting while the task runs autonomously. When it finishes, the result flows back into your conversation.

```text
You:   "Research the top 5 competitors and write a summary"
Bot:   → delegates to background task, you keep chatting
Bot:   → task finishes, result appears in your chat
```

Each task gets its own memory file (`TASKMEMORY.md`) and can be resumed with follow-ups.

### 5. Sub-agents (fully isolated second agent)

Sub-agents are completely separate bots — own chat, own workspace, own memory, own CLI auth, own config settings. Each sub-agent can use a different transport.

```bash
albert agents add codex-agent    # creates a new bot (needs its own token)
```

Sub-agents live under `~/.albert/agents/<name>/` with their own workspace, tools, and memory — fully isolated from the main agent.

### Comparison

| | Single chat | Group topics | Named sessions | Background tasks | Sub-agents |
|---|---|---|---|---|---|
| **What it is** | Your main 1:1 chat | One topic = one chat | Extra context in any chat | "Do this while I keep working" | Separate bot, own everything |
| **Context** | One per provider | One per topic per provider | Own context per session | Own context, result flows back | Fully isolated |
| **Workspace** | `~/.albert/` | Shared with main | Shared with parent chat | Shared with parent agent | Own under `~/.albert/agents/` |
| **Config** | Main config | Shared with main | Shared with parent chat | Shared with parent agent | Own config |
| **Setup** | Automatic | Create group + enable topics | `/session <prompt>` | Automatic or "delegate this" | `albert agents add` |

### How it all fits together

```text
~/.albert/                          ← shared workspace (tools, memory, files)
  │
  ├── Single chat                   ← main agent, private 1:1
  │     ├── main context
  │     └── named sessions
  │
  ├── Group: "My Projects"          ← same agent, same workspace
  │     ├── General (own context)
  │     ├── Topic: Auth (own context, own model)
  │     ├── Topic: Frontend (own context)
  │     └── each topic can have named sessions too
  │
  └── agents/codex-agent/           ← sub-agent, fully isolated workspace
        ├── own single chat
        ├── own group support
        ├── own named sessions
        └── own background tasks
```

## Features

- **Multi-transport** — Telegram, Discord, and Matrix simultaneously, or pick one
- **Multi-language** — UI in English, Deutsch, Nederlands, Français, Русский, Español, Português
- **Real-time streaming** — live message edits (Telegram) or segment-based output (Matrix)
- **Provider switching** — `/model` to change provider/model (never blocks, even during active processes)
- **Persistent memory** — plain Markdown files that survive across sessions
- **Cron jobs** — in-process scheduler with timezone support, per-job overrides, result routing to originating chat
- **Webhooks** — `wake` (inject into active chat) and `cron_task` (isolated task run) modes
- **Heartbeat** — proactive checks with per-target settings, group/topic support, chat validation
- **Image processing** — auto-resize and WebP conversion for incoming images (configurable)
- **Config hot-reload** — most settings update without restart (including language, scene, image)
- **Docker sandbox** — optional sidecar container with configurable host mounts
- **Service manager** — Linux (systemd), macOS (launchd), Windows (Task Scheduler)
- **Cross-tool skill sync** — shared skills across `~/.claude/`, `~/.codex/`, `~/.gemini/`

## Messenger support

Telegram is the primary transport — full feature set, battle-tested, zero extra dependencies.

| Messenger | Status | Streaming | Buttons |
|---|---|---|---|
| **Telegram** | primary | Live message edits | Inline keyboards |
| **Discord** | supported | Segment-based | Reactions |
| **Matrix** | supported | Segment-based (new messages) | Emoji reactions |

All transports can run **in parallel** on the same agent:

```json
{"transports": ["telegram", "discord"]}
```

### Plugin system for additional messengers

Each messenger is a self-contained module under `messenger/<name>/` implementing a shared `BotProtocol`. The core (orchestrator, sessions, CLI, cron, etc.) is completely transport-agnostic — it never knows which messenger delivered the message.

Adding a new messenger (Slack, Signal, ...) means implementing `BotProtocol` in a new sub-package and registering it — the rest of Albert works without changes.

## Auth

### Telegram

Albert uses a dual-allowlist model. Every message must pass both checks.

| Chat type | Check |
|---|---|
| **Private** | `user_id ∈ allowed_user_ids` |
| **Group** | `group_id ∈ allowed_group_ids` AND `user_id ∈ allowed_user_ids` |

All three settings are **hot-reloadable** — edit `config.json` and changes take effect within seconds.

> **Privacy Mode:** Telegram bots have Privacy Mode enabled by default and only see `/commands` in groups. To let the bot see all messages, make it a **group admin** or disable Privacy Mode via BotFather (`/setprivacy` → Disable).

### Matrix

Matrix auth uses room and user allowlists in the `matrix` config block. The bot logs in on first start, then persists `access_token` and `device_id` for subsequent runs.

## Language

Albert's UI is available in multiple languages. Set in `config.json`:

```json
{"language": "ru"}
```

Supported: `en`, `de`, `nl`, `fr`, `ru`, `es`, `pt`. Hot-reloadable.

## Commands

| Command | Description |
|---|---|
| `/model` | Interactive model/provider selector |
| `/new` | Reset active provider session |
| `/stop` | Stop current message and discard queued messages |
| `/interrupt` | Interrupt current message, queued messages continue |
| `/stop_all` | Kill everything — all messages, sessions, tasks, all agents |
| `/status` | Session/provider/auth status |
| `/memory` | Show persistent memory |
| `/session <prompt>` | Start a named background session |
| `/sessions` | View/manage active sessions |
| `/tasks` | View/manage background tasks |
| `/cron` | Interactive cron management |
| `/showfiles` | Browse `~/.albert/` |
| `/diagnose` | Runtime diagnostics |
| `/upgrade` | Check/apply updates |
| `/agents` | Multi-agent status |
| `/agent_commands` | Multi-agent command reference |
| `/where` | Show tracked chats/groups |
| `/info` | Version + links |

## Common CLI commands

```bash
albert                  # Start bot (auto-onboarding if needed)
albert onboarding       # Re-run setup wizard
albert reset            # Full reset + onboarding
albert stop             # Stop bot
albert restart          # Restart bot
albert upgrade          # Upgrade and restart
albert status           # Runtime status
albert help             # CLI overview
albert uninstall        # Remove bot + workspace

albert service install  # Install as background service
albert service status   # Show service status
albert service start    # Start service
albert service stop     # Stop service
albert service logs     # View service logs
albert service uninstall

albert docker enable    # Enable Docker sandbox
albert docker rebuild   # Rebuild sandbox container
albert docker mount /p  # Add host mount

albert agents list      # List configured sub-agents
albert agents add NAME  # Add a sub-agent
albert agents remove NAME

albert install matrix   # Install Matrix transport extra
albert install api      # Install API extra
```

## Workspace layout

```text
~/.albert/
  config/config.json                 # Bot configuration
  sessions.json                      # Chat session state
  tasks.json                         # Background task registry
  cron_jobs.json                     # Scheduled tasks
  agents.json                        # Sub-agent registry (optional)
  SHAREDMEMORY.md                    # Shared knowledge across all agents
  CLAUDE.md / AGENTS.md / GEMINI.md  # Rule files
  logs/
  workspace/
    memory_system/MAINMEMORY.md      # Persistent memory
    cron_tasks/ skills/ tools/       # Scripts and tools
    tasks/                           # Per-task folders
    telegram_files/ matrix_files/    # Media files (per transport)
    output_to_user/                  # Generated deliverables
  agents/<name>/                     # Sub-agent workspaces (isolated)
```

Full config reference: [`docs/config.md`](docs/config.md) — full example: [`config.example.json`](config.example.json)

## Why Albert?

Other projects manipulate SDKs or patch CLIs and risk violating provider terms of service. Albert simply runs the official CLI binaries as subprocesses — nothing more.

- Official CLIs only (`claude`, `codex`, `gemini`)
- Rule files are plain Markdown (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`)
- Memory is one Markdown file per agent
- All state is JSON — no database, no external services

## Disclaimer

Albert runs official provider CLIs and does not impersonate provider clients. Validate your own compliance requirements before unattended automation.

- [Anthropic Terms](https://www.anthropic.com/policies/terms)
- [OpenAI Terms](https://openai.com/policies/terms-of-use)
- [Google Terms](https://policies.google.com/terms)

## License

[MIT](LICENSE)
