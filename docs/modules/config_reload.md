# config_reload.py

Centralized runtime config hot-reload watcher.

## File

- `ductor_bot/config_reload.py`: `ConfigReloader`, config diff/classification helpers

## Purpose

Avoid unnecessary restarts when safe `config.json` fields change.

`ConfigReloader` polls `~/.ductor/config/config.json` every 5 seconds, validates with `AgentConfig`, diffs top-level schema fields, and:

- applies hot-reloadable fields immediately,
- logs restart-required field changes through callback.

## Public pieces

- `diff_configs(old, new) -> dict[field, (old, new)]`
- `classify_changes(changes) -> (hot_values, restart_fields)`
- `ConfigReloader.start() / stop()`

## Hot-reloadable fields

- `model`, `provider`, `reasoning_effort`
- `cli_timeout`, `max_budget_usd`, `max_turns`, `max_session_messages`
- `idle_timeout_minutes`, `session_age_warning_hours`, `daily_reset_hour`, `daily_reset_enabled`
- `permission_mode`, `file_access`, `user_timezone`
- `streaming`, `heartbeat`, `cleanup`, `cli_parameters`

Important runtime nuance:

- `heartbeat` and `cleanup` values are hot-applied to config objects, but observer lifecycle is not toggled on reload.
- if heartbeat/cleanup were disabled at startup, switching `enabled=true` requires restart.
- when already running, updated values are picked up on subsequent loop cycles.

## Restart-required fields

- `telegram_token`, `allowed_user_ids`
- `docker`, `api`, `webhooks`
- `ductor_home`, `log_level`, `gemini_api_key`
- restart classification is schema-based over `AgentConfig` top-level fields

## Wiring

`Orchestrator.create()` creates and starts the reloader with:

- `on_hot_reload` -> `Orchestrator._on_config_hot_reload(...)`
- `on_restart_needed` -> warning logger callback

`Orchestrator.shutdown()` stops it via `_stop_observers()`.
