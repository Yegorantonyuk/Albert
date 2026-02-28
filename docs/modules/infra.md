# infra/

Runtime infrastructure: process lifecycle, restart/update flow, Docker sandbox, service backends.

## Files

- `pidlock.py`: single-instance PID lock
- `restart.py`: restart marker/sentinel helpers, `EXIT_RESTART = 42`
- `docker.py`: `DockerManager`
- `install.py`: install mode detection (`pipx` / `pip` / `dev`)
- `platform.py`: shared platform helpers (`is_windows`)
- `process_tree.py`: cross-platform process-tree terminate/kill helpers
- `service.py`: platform dispatch facade
- `service_base.py`: shared console helper, NVM path collection
- `service_logs.py`: shared log rendering (`print_recent_logs`, file/journal adapters)
- `service_linux.py`: Linux systemd backend
- `service_macos.py`: macOS launchd backend
- `service_windows.py`: Windows Task Scheduler backend
- `version.py`: PyPI version/changelog utilities
- `updater.py`: `UpdateObserver`, upgrade helpers/sentinel

## Service management

`service.py` dispatches by platform:

- Linux -> systemd user service (`service_linux.py`)
- macOS -> launchd Launch Agent (`service_macos.py`)
- Windows -> Task Scheduler (`service_windows.py`)

Shared helpers:

- `ensure_console()` in `service_base.py`
- `print_recent_logs()` in `service_logs.py`
- `print_file_service_logs()` for file-based backends (macOS/Windows)
- `print_journal_service_logs()` for journalctl backend (Linux)

`print_recent_logs()` behavior:

- prefers `~/.ductor/logs/agent.log`
- fallback: newest `*.log`
- prints last 50 lines by default

### Linux backend

- service file: `~/.config/systemd/user/ductor.service`
- optional linger enable via `sudo loginctl enable-linger <user>`
- logs command uses `journalctl --user -u ductor -f`

### macOS backend

- plist: `~/Library/LaunchAgents/dev.ductor.plist`
- launchd logs configured to `service.log` / `service.err`
- `ductor service logs` uses `print_file_service_logs()` over ductor log files

### Windows backend

- scheduled task name: `ductor`
- starts 10s after logon
- restart-on-failure policy: up to 3 retries, 1 minute interval
- prefers `pythonw.exe -m ductor_bot`, fallback `ductor` binary
- explicit admin hint panel on access-denied `schtasks` errors
- `ductor service logs` uses `print_file_service_logs()`

## PID lock

`acquire_lock(pid_file, kill_existing=True)` is used for bot startup.

- detects stale/alive PID
- optionally terminates existing process
- writes current PID

Windows compatibility includes broader `OSError` handling around PID liveness/termination checks.

## Process-tree helpers

`process_tree.py` centralizes process termination behavior used across CLI execution and runtime control:

- POSIX: sends signals to process trees (`SIGTERM` then `SIGKILL`)
- Windows: uses `taskkill /T` (`/F` for force-kill)
- `kill_all_ductor_processes()` (Windows-only) additionally removes lingering `ductor.exe` and pipx venv `python/pythonw` processes to avoid upgrade file-lock issues

## Restart protocol

- `/restart` or restart marker file triggers exit code `42`
- restart sentinel stores chat + message for post-restart notification
- sentinel consumed on next startup

`ductor stop` (`__main__._stop_bot`) behavior:

1. stop installed background service (prevents immediate respawn)
2. kill PID-file instance
3. on Windows only: kill remaining ductor processes (`kill_all_ductor_processes`)
4. short Windows wait for lock release
5. stop Docker container when enabled

## Docker manager

`DockerManager.setup()`:

1. verify Docker binary/daemon
2. ensure image (build when missing and `auto_build=true`)
3. reuse running container or start new one
4. mount `~/.ductor -> /ductor`
5. mount provider homes when present:
   - `~/.claude`
   - `~/.codex`
   - `~/.gemini`
   - `~/.claude.json` (file mount)
6. optionally mount host cache (`mount_host_cache` config flag):
   - Linux: `~/.cache` (or `$XDG_CACHE_HOME`)
   - macOS: `~/Library/Caches`
   - Windows: `%LOCALAPPDATA%`
7. mount user-configured directories from `docker.mounts`:
   - host paths are expanded/resolved and must exist as directories
   - mounted as `rw` under `/mnt/<name>`
   - `<name>` comes from host basename (sanitized), with `_2`, `_3` suffixes for collisions

Linux adds UID/GID mapping (`--user uid:gid`) to avoid root-owned host files.

If setup fails, orchestrator falls back to host execution.
At runtime, `Orchestrator._ensure_docker()` also health-checks the container and falls back to host execution if recovery fails.

The `Dockerfile.sandbox` includes Chrome/Chromium runtime dependencies (libgbm, libnss3, libasound2, etc.) for browser-based skills using patchright/playwright.

### Docker CLI commands

| Command | Effect |
|---|---|
| `ductor docker` | Show docker subcommand help |
| `ductor docker rebuild` | Stop bot, remove container & image (rebuilt on next start) |
| `ductor docker enable` | Set `docker.enabled = true` in config |
| `ductor docker disable` | Stop container, set `docker.enabled = false` in config |
| `ductor docker mount <path>` | Add directory to `docker.mounts` (stored as resolved absolute path) |
| `ductor docker unmount <path>` | Remove configured mount (exact/resolved/basename match) |
| `ductor docker mounts` | List configured mounts with resolved container targets and status |

`ductor docker rebuild` calls `_stop_bot()` first, which also stops a running installed service. Rebuild does not explicitly start the service again.

Mount-management commands update `config.json` only. Restart (or rebuild) is required for new `docker run -v ...` flags to take effect.

## Version/update system

- `check_pypi()` fetches latest package metadata
- `UpdateObserver` checks periodically and notifies once per new version
- `check_pypi(fresh=True)` adds cache-busting and no-cache headers for manual `/upgrade` checks
- `perform_upgrade_pipeline()` runs generic upgrade, verifies installed version with short settle polling, and performs one forced retry when needed
- upgrade sentinel stores old/new version + chat for post-restart confirmation

## Supervisor

See [supervisor.md](supervisor.md) for the in-process multi-agent supervisor (`ductor_bot/multiagent/supervisor.py`).
