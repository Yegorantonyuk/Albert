"""Discord transport bot, parallel to TelegramBot and MatrixBot.

Implements BotProtocol so the supervisor can manage it identically
to other transports without knowing which one is active.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.commands import BOT_COMMANDS, MULTIAGENT_SUB_COMMANDS
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.i18n import t
from ductor_bot.infra.version import get_current_version
from ductor_bot.messenger.commands import classify_command
from ductor_bot.messenger.discord.sender import (
    DISCORD_MAX_CHARS,
    DiscordSendOpts,
    send_discord_message,
    split_discord_text,
)
from ductor_bot.messenger.notifications import NotificationService
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    import discord

    from ductor_bot.infra.updater import UpdateObserver
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_TRANSPORT_KEY = "dc"


def _expand_marker(ductor_home: str) -> Path:
    return Path(ductor_home).expanduser() / "restart-requested"


# ---------------------------------------------------------------------------
# Streaming editor
# ---------------------------------------------------------------------------


class DiscordStreamEditor:
    """Buffers streaming text deltas and periodically edits a Discord message.

    Mirrors the role of ``MatrixStreamEditor``.  Because Discord enforces
    rate limits on message edits, edits are throttled to at most one per
    ``edit_interval_seconds``.
    """

    def __init__(
        self,
        channel: discord.abc.Messageable,
        *,
        edit_interval_seconds: float = 2.0,
    ) -> None:
        self._channel = channel
        self._edit_interval = edit_interval_seconds
        self._buffer = ""
        self._message: discord.Message | None = None
        self._last_edit: float = 0.0
        self._finalized = False

    async def on_delta(self, delta: str) -> None:
        """Accumulate a text delta; send/edit the message as needed."""
        self._buffer += delta
        now = time.monotonic()

        if self._message is None:
            # Send the first chunk to create the message.
            try:
                self._message = await self._channel.send(self._buffer or "\u2026")
                self._last_edit = now
            except Exception:
                logger.exception("Discord streaming: failed to send initial message")
            return

        if now - self._last_edit >= self._edit_interval:
            await self._do_edit()
            self._last_edit = now

    async def on_tool(self, text: str) -> None:
        """Tool activity: suppressed in Discord to keep the chat clean."""

    async def on_system(self, text: str) -> None:
        """System status: suppressed in Discord."""

    async def finalize(self, final_text: str | None) -> None:
        """Flush the final text, replacing the in-progress message."""
        if self._finalized:
            return
        self._finalized = True

        text = final_text if final_text is not None else self._buffer
        if not text:
            return

        chunks = split_discord_text(text)

        if self._message is not None:
            # Replace the in-progress message with the first final chunk.
            try:
                await self._message.edit(content=chunks[0])
            except Exception:
                logger.exception("Discord streaming: finalize edit failed")
            # Send overflow chunks as new messages.
            for chunk in chunks[1:]:
                try:
                    await self._channel.send(chunk)
                except Exception:
                    logger.exception("Discord streaming: overflow chunk send failed")
        else:
            # No initial message was sent yet; send everything now.
            for chunk in chunks:
                try:
                    await self._channel.send(chunk)
                except Exception:
                    logger.exception("Discord streaming: final send failed")

    async def _do_edit(self) -> None:
        if self._message is None or not self._buffer:
            return
        content = self._buffer[:DISCORD_MAX_CHARS]
        try:
            await self._message.edit(content=content)
        except Exception:
            logger.debug("Discord streaming: edit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Notification service
# ---------------------------------------------------------------------------


class DiscordNotificationService:
    """NotificationService implementation for Discord."""

    def __init__(self, bot: DiscordBot) -> None:
        self._bot = bot

    async def notify(self, chat_id: int, text: str) -> None:
        channel = await self._bot._fetch_channel_as_messageable(chat_id)
        if channel is not None:
            await send_discord_message(channel, text)
        else:
            logger.warning(
                "notify: cannot resolve channel_id=%d, falling back to notify_all", chat_id
            )
            await self.notify_all(text)

    async def notify_all(self, text: str) -> None:
        for ch_id in self._bot._broadcast_channel_ids():
            channel = await self._bot._fetch_channel_as_messageable(ch_id)
            if channel is not None:
                await send_discord_message(channel, text)


# ---------------------------------------------------------------------------
# Internal discord.Client subclass
# ---------------------------------------------------------------------------


class _DiscordClient:
    """Thin discord.Client subclass that delegates events to DiscordBot.

    Defined as an inner class to avoid top-level ``import discord`` that
    would break imports when discord.py is not installed.
    """

    @staticmethod
    def create(bot: DiscordBot) -> discord.Client:
        """Build and return a discord.Client wired to *bot*."""
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # requires enabling in Discord Developer Portal

        class _Inner(discord.Client):
            def __init__(self, inner_bot: DiscordBot, **kwargs: object) -> None:
                super().__init__(**kwargs)
                self._bot_ref = inner_bot

            async def on_ready(self) -> None:
                await self._bot_ref._on_ready()

            async def on_message(self, message: discord.Message) -> None:
                await self._bot_ref._on_message(message)

        return _Inner(bot, intents=intents)


# ---------------------------------------------------------------------------
# DiscordBot
# ---------------------------------------------------------------------------


class DiscordBot:
    """Discord transport bot implementing BotProtocol."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
        bus: MessageBus | None = None,
        lock_pool: LockPool | None = None,
    ) -> None:
        try:
            import discord  # noqa: F401
        except ImportError:
            raise ImportError(
                "discord.py is required for Discord transport. "
                "Install with: pip install 'albert[discord]'"
            ) from None

        self._config = config
        self._agent_name = agent_name
        self._lock_pool = lock_pool or LockPool()
        self._bus = bus or MessageBus(lock_pool=self._lock_pool)

        self._client: discord.Client = _DiscordClient.create(self)

        from ductor_bot.messenger.discord.transport import DiscordTransport

        self._bus.register_transport(DiscordTransport(self))

        self._orchestrator: Orchestrator | None = None
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._notification_service: NotificationService = DiscordNotificationService(self)
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None
        self._exit_code: int = 0
        self._update_observer: UpdateObserver | None = None
        self._restart_watcher: asyncio.Task[None] | None = None

        # Pre-build allowed sets for O(1) lookup.
        dc = config.discord
        self._allowed_user_ids: frozenset[int] = frozenset(dc.allowed_user_ids)
        self._allowed_guild_ids: frozenset[int] = frozenset(dc.allowed_guild_ids)
        self._allowed_channel_ids: frozenset[int] = frozenset(dc.allowed_channel_ids)

        # Keep fire-and-forget tasks alive until completion.
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Block message processing until on_ready fires.
        self._ready = False

        # Last channel that sent a message (broadcast fallback).
        self._last_active_channel_id: int | None = None

    # --- BotProtocol implementation ---

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized — call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        self._abort_all_callback = callback

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def run(self) -> int:
        """Connect to Discord and run the event loop.  Blocks until shutdown."""
        token = self._config.discord.token
        if not token:
            logger.error("Discord: no token configured (discord.token is empty)")
            return 1

        try:
            await self._client.start(token)
        except Exception as exc:
            import discord

            if isinstance(exc, discord.LoginFailure):
                logger.error("Discord: invalid token — login failed: %s", exc)
                return 1
            if not isinstance(exc, asyncio.CancelledError):
                logger.exception("Discord client exited with error, requesting restart")
                from ductor_bot.infra.restart import EXIT_RESTART

                self._exit_code = EXIT_RESTART

        return self._exit_code

    async def shutdown(self) -> None:
        """Gracefully shut down the bot."""
        if self._restart_watcher:
            self._restart_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_watcher

        if self._update_observer:
            await self._update_observer.stop()

        await self._client.close()

        if self._orchestrator:
            await self._orchestrator.shutdown()

        logger.info("DiscordBot shut down")

    # --- Task management ---

    def _spawn_task(self, coro: Coroutine[object, object, None], *, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # --- Event handlers ---

    async def _on_ready(self) -> None:
        """Called by the discord.Client when the connection is established."""
        from ductor_bot.messenger.discord.startup import run_discord_startup

        await run_discord_startup(self)
        self._ready = True
        self._restart_watcher = asyncio.create_task(self._watch_restart_marker())
        logger.info("Discord bot ready as %s", self._client.user)

    async def _on_message(self, message: discord.Message) -> None:
        """Handle an incoming Discord message."""
        if not self._ready:
            return
        if message.author == self._client.user:
            return  # Never process own messages.

        if not self._is_authorized(message):
            return

        text = message.content.strip()
        if not text:
            return

        channel_id = message.channel.id
        self._last_active_channel_id = channel_id

        if text.startswith(("/", "!")):
            await self._handle_command(text, message)
            return

        key = SessionKey.for_transport(_TRANSPORT_KEY, channel_id)
        self._spawn_task(
            self._dispatch_with_lock(key, text, message),
            name=f"dc-msg-{channel_id}",
        )

    # --- Authorization ---

    def _is_authorized(self, message: discord.Message) -> bool:
        """Return True when the message passes all configured allow-lists."""
        if self._allowed_user_ids and message.author.id not in self._allowed_user_ids:
            return False
        if self._allowed_guild_ids:
            if message.guild is None or message.guild.id not in self._allowed_guild_ids:
                return False
        if self._allowed_channel_ids and message.channel.id not in self._allowed_channel_ids:
            return False
        return True

    # --- Command handling ---

    async def _handle_command(self, text: str, message: discord.Message) -> None:
        """Dispatch a command (``/cmd`` or ``!cmd`` prefix)."""
        channel_id = message.channel.id
        key = SessionKey.for_transport(_TRANSPORT_KEY, channel_id)

        # Normalize: strip leading prefix; keep ``/`` for orchestrator compat.
        cmd = text.split(maxsplit=1)[0].lower().lstrip("/!")
        if text.startswith("!"):
            text = "/" + text[1:]

        handler = self._COMMAND_DISPATCH.get(cmd)
        if handler is not None:
            if cmd in self._IMMEDIATE_COMMANDS:
                await handler(self, text=text, channel=message.channel, key=key, message=message)
            else:
                self._spawn_task(
                    self._run_handler_with_lock(
                        handler, text=text, channel=message.channel, key=key, message=message
                    ),
                    name=f"dc-cmd-{cmd}",
                )
        elif classify_command(cmd) in ("orchestrator", "multiagent"):
            self._spawn_task(
                self._cmd_orchestrator_locked(
                    text=text, channel=message.channel, key=key, message=message
                ),
                name=f"dc-orch-{cmd}",
            )
        else:
            # Unknown command → treat as regular message.
            self._spawn_task(
                self._dispatch_with_lock(key, text, message),
                name=f"dc-unknown-{cmd}",
            )

    # -- Individual command handlers ----------------------------------------

    async def _cmd_stop(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        if orch:
            killed = await orch.abort(key.chat_id)
            msg = t("abort_all.done", count=killed) if killed else t("abort_all.nothing")
        else:
            msg = t("abort_all.nothing")
        await self._send_rich(channel, msg)

    async def _cmd_interrupt(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        if orch:
            interrupted = orch.interrupt(key.chat_id)
            msg = t("interrupt.done", count=interrupted) if interrupted else t("interrupt.nothing")
            await self._send_rich(channel, msg)

    async def _cmd_stop_all(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        killed = 0
        if orch:
            killed = await orch.abort_all()
        if self._abort_all_callback:
            killed += await self._abort_all_callback()
        msg = t("abort_all.done", count=killed) if killed else t("abort_all.nothing")
        await self._send_rich(channel, msg)

    async def _cmd_restart(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART, write_restart_marker

        marker = _expand_marker(self._config.ductor_home)
        write_restart_marker(marker_path=marker)
        await self._send_rich(
            channel,
            fmt(t("startup.restart_header"), SEP, t("startup.restart_body")),
        )
        self._exit_code = EXIT_RESTART
        await self._client.close()

    async def _cmd_new(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        if orch:
            result = await orch.handle_message(key, "/new")
            if result and result.text:
                await self._send_rich(channel, result.text)

    async def _cmd_help(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        await self._send_rich(channel, self._build_help_text())

    async def _cmd_info(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        version = get_current_version()
        text_out = fmt(
            t("info.header"),
            t("info.version", version=version),
            SEP,
            t("info.discord_description"),
        )
        await self._send_rich(channel, text_out)

    async def _cmd_agent_commands(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        lines = [
            t("agents.discord_explanation"),
            "",
            t("agents.commands_header"),
            "`!agents` — list all agents and their status",
            "`!agent_start <name>` — start a sub-agent",
            "`!agent_stop <name>` — stop a sub-agent",
            "`!agent_restart <name>` — restart a sub-agent",
            "",
            t("agents.setup_header"),
            t("agents.setup_instruction"),
        ]
        await self._send_rich(
            channel,
            fmt(t("agents.system_header"), SEP, "\n".join(lines)),
        )

    async def _cmd_showfiles(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        if not orch:
            return

        from ductor_bot.messenger.matrix.file_browser import format_file_listing

        parts = text.split(None, 1)
        subdir = parts[1].strip() if len(parts) > 1 else ""

        listing = await asyncio.to_thread(format_file_listing, orch.paths, subdir)
        await self._send_rich(channel, listing)

    async def _cmd_session(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await self._send_rich(
                channel,
                fmt(
                    t("session_help.header"),
                    SEP,
                    f"{t('session_help.discord_session_cmd')}\n"
                    f"{t('session_help.discord_sessions_cmd')}\n"
                    f"{t('session_help.discord_stop_cmd')}",
                ),
            )
            return
        # Route to orchestrator as a conversation message.
        await self._dispatch_message(key, text, message)

    async def _cmd_orchestrator(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        orch = self._orchestrator
        if not orch:
            return
        result = await orch.handle_message(key, text)
        if result and result.text:
            await self._send_rich(channel, result.text)

    async def _dispatch_with_lock(
        self, key: SessionKey, text: str, message: discord.Message
    ) -> None:
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await self._dispatch_message(key, text, message)

    async def _run_handler_with_lock(
        self, handler: Callable[..., Awaitable[None]], **kwargs: object
    ) -> None:
        key: SessionKey = kwargs["key"]  # type: ignore[assignment]
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await handler(self, **kwargs)

    async def _cmd_orchestrator_locked(
        self, *, text: str, channel: discord.abc.Messageable, key: SessionKey, message: object
    ) -> None:
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await self._cmd_orchestrator(text=text, channel=channel, key=key, message=message)

    async def _dispatch_message(
        self, key: SessionKey, text: str, message: discord.Message
    ) -> None:
        """Route a message through the streaming or non-streaming pipeline."""
        if self._config.streaming.enabled:
            await self._run_streaming(key, text, message)
        else:
            await self._run_non_streaming(key, text, message)

    async def _run_streaming(
        self, key: SessionKey, text: str, message: discord.Message
    ) -> None:
        orch = self._orchestrator
        if orch is None:
            return

        editor = DiscordStreamEditor(
            message.channel,  # type: ignore[arg-type]
            edit_interval_seconds=self._config.streaming.edit_interval_seconds,
        )
        result = await orch.handle_message_streaming(
            key,
            text,
            on_text_delta=editor.on_delta,
            on_tool_activity=editor.on_tool,
            on_system_status=editor.on_system,
        )
        self._maybe_append_footer(result)
        await editor.finalize(result.text)

    async def _run_non_streaming(
        self, key: SessionKey, text: str, message: discord.Message
    ) -> None:
        orch = self._orchestrator
        if orch is None:
            return

        result = await orch.handle_message(key, text)
        self._maybe_append_footer(result)
        if result.text:
            await self._send_rich(message.channel, result.text)  # type: ignore[arg-type]

    # Dispatch table: command name → handler method
    _COMMAND_DISPATCH: dict[str, Callable[..., Awaitable[None]]] = {
        "stop": _cmd_stop,
        "stop_all": _cmd_stop_all,
        "interrupt": _cmd_interrupt,
        "restart": _cmd_restart,
        "new": _cmd_new,
        "help": _cmd_help,
        "start": _cmd_help,
        "info": _cmd_info,
        "agent_commands": _cmd_agent_commands,
        "showfiles": _cmd_showfiles,
        "session": _cmd_session,
    }

    _IMMEDIATE_COMMANDS: frozenset[str] = frozenset(
        {
            "stop",
            "stop_all",
            "interrupt",
            "restart",
            "help",
            "start",
            "info",
            "agent_commands",
            "showfiles",
        }
    )

    def _build_help_text(self) -> str:
        cmd_desc = {**dict(BOT_COMMANDS), **dict(MULTIAGENT_SUB_COMMANDS)}

        def _line(c: str) -> str:
            desc = cmd_desc.get(c, "")
            return f"`!{c}` — {desc}" if desc else f"`!{c}`"

        return fmt(
            t("help.header"),
            SEP,
            f"**{t('help.cat_daily')}**\n{_line('new')}\n{_line('stop')}\n{_line('stop_all')}\n"
            f"{_line('model')}\n{_line('status')}\n{_line('memory')}",
            f"**{t('help.cat_automation')}**\n{_line('session')}\n{_line('tasks')}\n{_line('cron')}",
            f"**{t('help.cat_multiagent')}**\n{_line('agent_commands')}\n{_line('agents')}\n"
            f"{_line('agent_start')}\n{_line('agent_stop')}\n{_line('agent_restart')}",
            f"**{t('help.cat_browse')}**\n{_line('showfiles')}\n{_line('info')}\n{_line('help')}",
            f"**{t('help.cat_maintenance')}**\n{_line('diagnose')}\n{_line('upgrade')}\n"
            f"{_line('restart')}",
            SEP,
            t("help.discord_footer"),
        )

    def _maybe_append_footer(self, result: object) -> None:
        from ductor_bot.orchestrator.registry import OrchestratorResult

        if not isinstance(result, OrchestratorResult):
            return
        if not self._config.scene.technical_footer or not result.model_name:
            return
        from ductor_bot.text.response_format import format_technical_footer

        footer = format_technical_footer(
            result.model_name,
            result.total_tokens,
            result.input_tokens,
            result.cost_usd,
            result.duration_ms,
        )
        result.text += footer

    # --- Sending helpers ---

    async def _send_rich(self, channel: discord.abc.Messageable, text: str) -> None:
        opts = DiscordSendOpts(allowed_roots=self._file_roots())
        await send_discord_message(channel, text, opts)

    def _file_roots(self) -> list[Path] | None:
        if self._orchestrator:
            return self.file_roots(self._orchestrator.paths)
        return None

    # --- Channel resolution ---

    async def _fetch_channel_as_messageable(
        self, channel_id: int
    ) -> discord.abc.Messageable | None:
        """Fetch a Discord channel by ID, returning it as Messageable or None."""
        import discord

        ch = self._client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(channel_id)
            except Exception:
                logger.warning("Discord: cannot resolve channel %d", channel_id)
                return None
        if isinstance(ch, discord.abc.Messageable):
            return ch
        logger.warning("Discord: channel %d is not Messageable: %r", channel_id, ch)
        return None

    def _broadcast_channel_ids(self) -> list[int]:
        """Return channels for broadcast delivery."""
        ids = list(self._config.discord.allowed_channel_ids)
        if not ids and self._last_active_channel_id:
            ids = [self._last_active_channel_id]
        return ids

    def _default_channel_id(self) -> int:
        """Default delivery target: first allowed channel or last active."""
        if self._config.discord.allowed_channel_ids:
            return self._config.discord.allowed_channel_ids[0]
        if self._last_active_channel_id:
            return self._last_active_channel_id
        logger.warning("Discord: no default channel_id; no allowed_channel_ids and no active chat")
        return 0

    # --- Inter-agent & task handlers (BotProtocol) ---

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        from ductor_bot.bus.adapters import from_interagent_result

        chat_id = self._default_channel_id()
        if not chat_id:
            logger.warning(
                "Discord: no channel_id for async interagent result (task=%s)", result.task_id
            )
            text = result.result_text or f"Inter-agent result from {result.recipient}"
            await self._notification_service.notify_all(text)
            return
        await self._bus.submit(from_interagent_result(result, chat_id))

    async def on_task_result(self, result: TaskResult) -> None:
        from ductor_bot.bus.adapters import from_task_result

        await self._bus.submit(from_task_result(result))

    async def on_task_question(
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
    ) -> None:
        from ductor_bot.bus.adapters import from_task_question

        if not chat_id:
            chat_id = self._default_channel_id()
        await self._bus.submit(from_task_question(task_id, question, prompt_preview, chat_id))

    # --- Restart watcher ---

    async def _watch_restart_marker(self) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART

        marker = _expand_marker(self._config.ductor_home)
        while True:
            await asyncio.sleep(2)
            if marker.exists():
                logger.info("Discord: restart marker detected")
                self._exit_code = EXIT_RESTART
                await self._client.close()
                return
