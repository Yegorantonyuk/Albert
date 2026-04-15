"""Discord delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Discord messages, mirroring
the structure of ``messenger/matrix/transport.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.discord.sender import DiscordSendOpts, send_discord_message
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    import discord

    from ductor_bot.messenger.discord.bot import DiscordBot

logger = logging.getLogger(__name__)


class DiscordTransport:
    """Implements the ``TransportAdapter`` protocol for Discord delivery."""

    def __init__(self, bot: DiscordBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    @property
    def transport_name(self) -> str:
        return "dc"

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target channel."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("Discord: no handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed channels."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning(
                "Discord: no broadcast handler for origin=%s", envelope.origin.value
            )

    # -- Internal helpers ---------------------------------------------------

    async def _resolve_channel(self, env: Envelope) -> discord.abc.Messageable | None:
        """Resolve envelope chat_id to a Discord channel."""
        return await self._bot._fetch_channel_as_messageable(env.chat_id)

    def _opts(self, env: Envelope) -> DiscordSendOpts:
        orch = self._bot.orchestrator
        roots: list[Path] | None = self._bot.file_roots(orch.paths) if orch else None
        return DiscordSendOpts(allowed_roots=roots)

    async def _send(self, env: Envelope, text: str) -> None:
        """Resolve channel and send text."""
        channel = await self._resolve_channel(env)
        if channel is None:
            return
        await send_discord_message(channel, text, self._opts(env))

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:
        elapsed = f"{env.elapsed_seconds:.0f}s"
        if env.session_name:
            if env.status == "aborted":
                text = fmt(
                    f"**[{env.session_name}] Cancelled**", SEP, f"_{env.prompt_preview}_"
                )
            elif env.is_error:
                body = env.result_text[:2000] if env.result_text else "_No output._"
                text = fmt(f"**[{env.session_name}] Failed** ({elapsed})", SEP, body)
            else:
                text = fmt(
                    f"**[{env.session_name}] Complete** ({elapsed})",
                    SEP,
                    env.result_text or "_No output._",
                )
        else:
            task_id = env.metadata.get("task_id", "?")
            if env.status == "aborted":
                text = fmt(
                    "**Background Task Cancelled**",
                    SEP,
                    f"Task `{task_id}` was cancelled.\nPrompt: _{env.prompt_preview}_",
                )
            elif env.is_error:
                text = fmt(
                    f"**Background Task Failed** ({elapsed})",
                    SEP,
                    f"Task `{task_id}` failed ({env.status}).\n"
                    f"Prompt: _{env.prompt_preview}_\n\n"
                    + (env.result_text[:2000] if env.result_text else "_No output._"),
                )
            else:
                text = fmt(
                    f"**Background Task Complete** ({elapsed})",
                    SEP,
                    env.result_text or "_No output._",
                )
        await self._send(env, text)

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        if env.result_text:
            await self._send(env, env.result_text)

    async def _deliver_interagent(self, env: Envelope) -> None:
        if env.is_error:
            session_info = f"\nSession: `{env.session_name}`" if env.session_name else ""
            text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{env.metadata.get('recipient', '?')}`{session_info}\n"
                f"Error: {env.metadata.get('error', 'unknown')}\n"
                f"Request: _{env.prompt_preview}_"
            )
            await self._send(env, text)
            return

        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            await self._send(env, f"**Provider Switch Detected**\n\n{notice}")
        if env.result_text:
            await self._send(env, env.result_text)

    async def _deliver_task_result(self, env: Envelope) -> None:
        name = env.metadata.get("name", env.metadata.get("task_id", "?"))

        note = ""
        if env.status == "done":
            duration = f"{env.elapsed_seconds:.0f}s"
            target = f"{env.provider}/{env.model}" if env.provider else ""
            detail = f"{duration}, {target}" if target else duration
            note = f"**Task `{name}` completed** ({detail})"
        elif env.status == "cancelled":
            note = f"**Task `{name}` cancelled**"
        elif env.status == "failed":
            note = f"**Task `{name}` failed**\nReason: {env.metadata.get('error', 'unknown')}"

        if note:
            await self._send(env, note)
        if env.needs_injection and env.result_text:
            await self._send(env, env.result_text)

    async def _deliver_task_question(self, env: Envelope) -> None:
        task_id = env.metadata.get("task_id", "?")
        note = f"**Task `{task_id}` has a question:**\n{env.prompt}"
        await self._send(env, note)
        if env.result_text:
            await self._send(env, env.result_text)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        if env.result_text:
            await self._send(env, env.result_text)

    async def _deliver_cron(self, env: Envelope) -> None:
        """Deliver cron result to a specific channel (unicast).

        Falls back to broadcast when the target channel cannot be resolved.
        """
        channel = await self._resolve_channel(env)
        if channel is None:
            logger.warning(
                "Discord cron unicast: cannot resolve chat_id=%d, falling back to broadcast",
                env.chat_id,
            )
            await self._broadcast_cron(env)
            return
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await send_discord_message(channel, text, self._opts(env))

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await self._broadcast(text, env)

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        text = (
            f"**WEBHOOK (CRON TASK): {title}**\n\n{env.result_text}"
            if env.result_text
            else f"**WEBHOOK (CRON TASK): {title}**\n\n_{env.status}_"
        )
        await self._broadcast(text, env)

    async def _broadcast(self, text: str, env: Envelope | None = None) -> None:
        """Send to all allowed channels (or last active channel as fallback)."""
        channel_ids = self._bot._broadcast_channel_ids()
        if not channel_ids:
            logger.warning("Discord _broadcast: no channels available, message lost: %s", text[:80])
            return
        opts = self._opts(env) if env is not None else DiscordSendOpts()
        for ch_id in channel_ids:
            channel = await self._bot._fetch_channel_as_messageable(ch_id)
            if channel is not None:
                await send_discord_message(channel, text, opts)


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[DiscordTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: DiscordTransport._deliver_background,
    Origin.CRON: DiscordTransport._deliver_cron,
    Origin.HEARTBEAT: DiscordTransport._deliver_heartbeat,
    Origin.INTERAGENT: DiscordTransport._deliver_interagent,
    Origin.TASK_RESULT: DiscordTransport._deliver_task_result,
    Origin.TASK_QUESTION: DiscordTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: DiscordTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.CRON: DiscordTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: DiscordTransport._broadcast_webhook_cron,
}
