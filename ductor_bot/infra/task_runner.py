"""Shared one-shot CLI task execution for cron, webhook, and background observers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.cron.execution import OneShotExecutionResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Normalized outcome of a one-shot task run."""

    status: str
    result_text: str
    execution: OneShotExecutionResult | None


async def run_oneshot_task(
    exec_config: TaskExecutionConfig,
    prompt: str,
    *,
    cwd: Path,
    timeout_seconds: float,
    timeout_label: str,
) -> TaskResult:
    """Build the CLI command and execute it, returning a normalized result.

    Returns a ``cli_not_found`` result instead of raising when the provider
    binary is missing.  All other execution details (timeout, stderr, status
    mapping) are delegated to ``execute_one_shot``.
    """
    from ductor_bot.cron.execution import build_cmd, execute_one_shot

    one_shot = build_cmd(exec_config, prompt)
    if one_shot is None:
        return TaskResult(
            status=f"error:cli_not_found_{exec_config.provider}",
            result_text=f"[{exec_config.provider} CLI not found]",
            execution=None,
        )

    execution = await execute_one_shot(
        one_shot,
        cwd=cwd,
        provider=exec_config.provider,
        timeout_seconds=timeout_seconds,
        timeout_label=timeout_label,
    )

    return TaskResult(
        status=execution.status,
        result_text=execution.result_text,
        execution=execution,
    )


async def check_folder(folder: Path) -> bool:
    """Return True if *folder* exists as a directory (runs in a thread)."""
    return await asyncio.to_thread(folder.is_dir)
