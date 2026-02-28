# background/

Named background session execution for `/session` (persistent sessions with follow-ups).

## Files

- `background/observer.py`: `BackgroundObserver` task lifecycle, execution, cancel/shutdown, result callback
- `background/models.py`: `BackgroundSubmit`, `BackgroundTask`, `BackgroundResult` dataclasses

## Purpose

Runs CLI tasks without blocking the chat flow, with persistent session support:

- user sends `/session <prompt>` (optionally `/session @provider <prompt>`)
- a named session is created (e.g. `swiftfox`)
- task executes asynchronously with session tracking
- bot sends a tagged completion/failure/cancel message when done
- follow-ups resume the same CLI session via `--resume`

## Execution model

`BackgroundObserver.submit(sub, exec_config)`:

1. enforces per-chat concurrency cap (`MAX_TASKS_PER_CHAT = 5`)
2. creates `BackgroundTask` metadata (`task_id`, chat/thread IDs, provider/model, prompt preview, session name)
3. starts `asyncio.create_task(self._run(...))`
4. auto-removes finished tasks from in-memory registry

Two execution paths:

- **Named session path** (`session_name` set): uses `CLIService.execute()` with `resume_session` for session persistence
- **Legacy path** (no `session_name`): uses `run_oneshot_task()` for stateless one-shot execution

## Status mapping

Delivered `BackgroundResult.status` values include:

- named-session success path: `ok`
- legacy one-shot success path: `success`
- named-session failures: `error:timeout`, `error:cli`, `error:internal`
- legacy one-shot failures: `error:timeout`, `error:exit_<code>`, `error:cli_not_found`, `error:internal`
- user abort path: `aborted`

## Wiring

Orchestrator integration (`orchestrator/core.py`):

- created in `Orchestrator.create(...)`
- submission API: `submit_named_session(...)`, `submit_named_followup_bg(...)`
- listing API: `active_background_tasks(...)`
- shared abort: `abort(chat_id)` cancels CLI subprocesses, cancels active background tasks, and ends all named sessions for that chat
- shutdown: `_stop_observers()` calls `BackgroundObserver.shutdown()`

Bot integration (`bot/app.py`):

- `/session` handler creates named session and confirms start to user
- result handler (`_on_session_result`) sends tagged completion/failure/cancel message
- `/sessions` shows interactive session management UI
- `/status` shows active background tasks via orchestrator status builder

## Limitations

- task registry is in-memory (lost on restart), but named sessions persist in JSON
- named session metadata persistence is handled by `session/named.py` (`~/.ductor/named_sessions.json`)
- no retry queue; each submission is a single execution
