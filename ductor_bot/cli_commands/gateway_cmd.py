"""Gateway management CLI subcommands (``albert gateway ...``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.workspace.paths import resolve_paths

_console = Console()

_SUBCOMMANDS = frozenset({"enable", "disable", "pair", "devices", "revoke"})
_REQUIRED_MODULES = ("nacl.signing", "jwt", "cryptography")


def _parse_subcommand(args: list[str]) -> str | None:
    found = False
    for arg in args:
        if arg.startswith("-"):
            continue
        if not found and arg == "gateway":
            found = True
            continue
        if found:
            return arg if arg in _SUBCOMMANDS else None
    return None


def _positional_after(args: list[str], subcommand: str) -> str | None:
    found = False
    for arg in args:
        if arg.startswith("-"):
            continue
        if arg == subcommand:
            found = True
            continue
        if found:
            return arg
    return None


def dependencies_available() -> bool:
    return all(find_spec(module) is not None for module in _REQUIRED_MODULES)


def _read_config() -> tuple[Path, dict[str, Any]] | None:
    paths = resolve_paths()
    if not paths.config_path.exists():
        _console.print("[red]Config not found.[/red] Run [bold]albert setup[/bold] first.")
        return None
    try:
        data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _console.print(f"[red]Could not read config:[/red] {exc}")
        return None
    return paths.config_path, data


def _gateway_base_url() -> str | None:
    result = _read_config()
    if result is None:
        return None
    _, data = result
    gateway = data.get("gateway", {})
    if not isinstance(gateway, dict) or not gateway.get("enabled"):
        _console.print("[yellow]Gateway is disabled.[/yellow] Run [bold]albert gateway enable[/bold].")
        return None
    host = gateway.get("host", "127.0.0.1")
    # Always reach the local instance over loopback: the configured host may be
    # a bind-all address, which is not a usable destination.
    reachable = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104
    return f"http://{reachable}:{gateway.get('port', 8750)}"


def _admin_secret() -> str | None:
    """Read the shared secret that authorizes operator-only endpoints."""
    from ductor_bot.gateway.tokens import load_or_create_secret

    paths = resolve_paths()
    try:
        return load_or_create_secret(paths.gateway_dir / "admin.secret")
    except OSError as exc:
        _console.print(f"[red]Could not read the admin secret:[/red] {exc}")
        return None


def print_help() -> None:
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=28)
    table.add_column()
    table.add_row("albert gateway enable", "Enable the app gateway")
    table.add_row("albert gateway disable", "Disable the app gateway")
    table.add_row("albert gateway pair", "Issue a one-time device pairing code")
    table.add_row("albert gateway devices", "List paired devices")
    table.add_row("albert gateway revoke <id>", "Revoke one device, or 'all'")

    result = _read_config()
    status = "not configured"
    if result is not None:
        gateway = result[1].get("gateway", {})
        if isinstance(gateway, dict) and gateway.get("enabled"):
            status = f"enabled on port {gateway.get('port', 8750)}"
        elif isinstance(gateway, dict):
            status = "disabled"

    _console.print(Panel(table, title=f"Gateway — {status}", border_style="cyan", padding=(1, 2)))
    _console.print()


def gateway_enable() -> None:
    """Turn the gateway on and warn about proxy configuration."""
    if not dependencies_available():
        _console.print(
            Panel(
                "The gateway needs extra packages.\n\n"
                "  [bold]pip install 'albert[gateway]'[/bold]",
                title="Missing dependencies",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
        return

    result = _read_config()
    if result is None:
        return
    config_path, data = result

    gateway = data.get("gateway", {})
    if not isinstance(gateway, dict):
        gateway = {}
    gateway["enabled"] = True
    gateway.setdefault("host", "127.0.0.1")
    gateway.setdefault("port", 8750)
    gateway.setdefault("trusted_proxies", [])
    data["gateway"] = gateway
    atomic_json_save(config_path, data)

    body = (
        f"Gateway will listen on [bold]{gateway['host']}:{gateway['port']}[/bold] "
        "after a restart.\n\n"
        "Next: [bold]albert gateway pair[/bold] to enrol your first device."
    )
    if not gateway["trusted_proxies"]:
        body += (
            "\n\n[yellow]If you put a reverse proxy in front of this gateway, add its "
            "address to [bold]gateway.trusted_proxies[/bold]. Until you do, pairing "
            "through a proxy is refused — the gateway cannot tell a real client from "
            "a forwarded one, and guessing would expose enrolment to the internet."
            "[/yellow]"
        )
    _console.print(Panel(body, title="Gateway enabled", border_style="green", padding=(1, 2)))


def gateway_disable() -> None:
    result = _read_config()
    if result is None:
        return
    config_path, data = result
    gateway = data.get("gateway", {})
    if not isinstance(gateway, dict):
        gateway = {}
    gateway["enabled"] = False
    data["gateway"] = gateway
    atomic_json_save(config_path, data)
    _console.print("[yellow]Gateway disabled.[/yellow] Restart to apply.")


async def _post(url: str, secret: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, json=payload, headers={"X-Albert-Admin": secret}) as response,
        ):
            if response.status != 200:
                _console.print(f"[red]Gateway returned HTTP {response.status}.[/red]")
                return None
            return dict(await response.json())
    except aiohttp.ClientError:
        _console.print(
            "[red]Could not reach the gateway.[/red] Is Albert running with the gateway enabled?"
        )
        return None


def gateway_pair() -> None:
    """Print a single-use pairing code for enrolling a new device."""
    import asyncio

    base = _gateway_base_url()
    secret = _admin_secret()
    if base is None or secret is None:
        return

    result = asyncio.run(_post(f"{base}/auth/pair-code", secret, {"label": "cli"}))
    if result is None:
        return

    _console.print(
        Panel(
            f"Pairing code:  [bold cyan]{result['code']}[/bold cyan]\n"
            f"Valid until:   {result['expires_at']}\n\n"
            "Enter it in the app while this machine is reachable over your LAN or\n"
            "Tailscale. The code is single-use and the device never needs that\n"
            "network again once paired.",
            title="Pair a device",
            border_style="cyan",
            padding=(1, 2),
        ),
    )


def gateway_devices() -> None:
    """List paired devices straight from the registry.

    Reads the file rather than the HTTP API so it keeps working when the
    gateway is stopped — which is exactly when an operator wants to look.
    """
    from ductor_bot.gateway.devices import DeviceRegistry

    paths = resolve_paths()
    devices = DeviceRegistry(paths.devices_path).list_devices()
    if not devices:
        _console.print("[yellow]No paired devices.[/yellow]")
        return

    table = Table(box=None, padding=(0, 2))
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Platform")
    table.add_column("Last seen")
    for device in devices:
        table.add_row(device.id, device.name, device.platform, device.last_seen_at or "never")
    _console.print()
    _console.print(table)
    _console.print()


def gateway_revoke(target: str | None) -> None:
    """Revoke one device, or every device when given 'all'."""
    from ductor_bot.gateway.devices import DeviceRegistry

    if not target:
        _console.print("[red]Specify a device id, or 'all'.[/red]")
        return

    registry = DeviceRegistry(resolve_paths().devices_path)
    if target == "all":
        count = registry.revoke_all()
        _console.print(f"[yellow]Revoked {count} device(s).[/yellow]")
        return

    if registry.revoke(target):
        _console.print(f"[yellow]Revoked {target}.[/yellow]")
    else:
        _console.print(f"[red]No active device with id {target}.[/red]")


def cmd_gateway(args: list[str]) -> None:
    """Handle ``albert gateway <subcommand>``."""
    sub = _parse_subcommand(args)
    if sub is None:
        print_help()
        return

    dispatch: dict[str, Callable[[], None]] = {
        "enable": gateway_enable,
        "disable": gateway_disable,
        "pair": gateway_pair,
        "devices": gateway_devices,
        "revoke": lambda: gateway_revoke(_positional_after(args, "revoke")),
    }
    _console.print()
    dispatch[sub]()
    _console.print()
