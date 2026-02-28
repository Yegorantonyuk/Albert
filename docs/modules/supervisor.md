# multiagent/supervisor.py

In-process multi-agent supervisor (`AgentSupervisor`) for main agent + optional sub-agents.

## File

- `ductor_bot/multiagent/supervisor.py`

## Purpose

`run_telegram()` always starts `AgentSupervisor`.

- main agent always runs under supervision
- sub-agents are loaded from `~/.ductor/agents.json`
- crash/restart policy is handled per agent task inside one asyncio process

## Startup lifecycle

`AgentSupervisor.start()`:

1. start `InterAgentBus`
2. start `InternalAgentAPI` (`127.0.0.1:8799`)
3. create/start main `AgentStack`
4. load + start sub-agents from `agents.json`
5. start `SharedKnowledgeSync` (`SHAREDMEMORY.md` -> agent memories)
6. start `agents.json` watcher
7. wait for main agent completion and return its exit code

## Supervision policy

Each agent runs in `_supervised_run(...)` with health tracking.

- normal exit: task ends
- exit code `42`:
  - sub-agent: in-process restart (stack rebuild)
  - main agent: propagate restart to process/service runtime
- crash: exponential backoff restarts (max 5 retries), then mark `crashed`

## Dynamic agent registry

`FileWatcher` polls `agents.json` (5s).

- added entry: start sub-agent
- removed entry: stop sub-agent
- changed `telegram_token`: restart sub-agent
- other config field changes in `agents.json` currently do not trigger auto-restart

## Orchestrator hook injection

During bot startup, supervisor injects hooks into each agent dispatcher.

- sets `orch._supervisor`
- on main agent: registers multi-agent commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`)

## Shutdown

`stop_all()` stops watcher/API/shared sync, cancels in-flight async inter-agent tasks, then stops sub-agents before main.
