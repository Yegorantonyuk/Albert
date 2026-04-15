"""Discord message sending utilities.

Handles plain-text messages, file attachments, and chunk splitting for
Discord's 2000-character message limit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.files.tags import path_from_file_tag
from ductor_bot.messenger.send_opts import BaseSendOpts

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

# Discord limit is 2000 chars; keep a small margin for safety.
DISCORD_MAX_CHARS = 1990
_FILE_TAG_RE = re.compile(r"<file:([^>]+)>")


@dataclass
class DiscordSendOpts(BaseSendOpts):
    """Options for sending a Discord message."""


async def send_discord_message(
    channel: discord.abc.Messageable,
    text: str,
    opts: DiscordSendOpts | None = None,
) -> discord.Message | None:
    """Send *text* to *channel*, splitting at the Discord character limit.

    Extracts ``<file:...>`` tags and uploads the referenced files.
    Returns the last :class:`discord.Message` sent, or ``None`` on failure.
    """
    import discord as _discord

    opts = opts or DiscordSendOpts()

    file_path_strs = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()

    last_msg: discord.Message | None = None

    if cleaned:
        for chunk in split_discord_text(cleaned):
            try:
                last_msg = await channel.send(chunk)
            except Exception:
                logger.exception("Discord: failed to send message chunk")

    for raw_path in file_path_strs:
        file_path = path_from_file_tag(raw_path)
        if not _file_accessible(file_path, opts.allowed_roots):
            continue
        try:
            last_msg = await channel.send(file=_discord.File(file_path))
        except Exception:
            logger.exception("Discord: failed to send file: %s", file_path)

    return last_msg


def split_discord_text(text: str) -> list[str]:
    """Split *text* into chunks that fit within Discord's 2000-char limit.

    Splits on newlines; handles individual lines longer than the limit by
    hard-splitting at the character boundary.
    """
    if len(text) <= DISCORD_MAX_CHARS:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        # Hard-split lines that are themselves too long.
        while len(line) > DISCORD_MAX_CHARS:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            chunks.append(line[:DISCORD_MAX_CHARS])
            line = line[DISCORD_MAX_CHARS:]

        line_len = len(line) + 1  # +1 for the joining newline
        if current_len + line_len > DISCORD_MAX_CHARS and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks or [""]


def _file_accessible(
    file_path: Path,
    allowed_roots: Sequence[Path] | None,
) -> bool:
    """Return *True* when *file_path* exists and is within *allowed_roots*."""
    if not file_path.exists():
        logger.warning("Discord: file not found: %s", file_path)
        return False
    if allowed_roots is not None and not any(
        file_path.resolve().is_relative_to(root.resolve()) for root in allowed_roots
    ):
        logger.warning("Discord: file outside allowed roots: %s", file_path)
        return False
    return True
