"""Top-level Typer entry point for the ``zata-ops`` CLI.

Mounts ``db``, ``env``, ``logs`` and ``dashboard`` sub-applications. Run
``zata-ops --help`` after installing via ``uv tool install`` for an overview.
"""

from __future__ import annotations

from typing import Optional

import typer

from zata_ops import __version__
from zata_ops.db import cli as db_cli
from zata_ops.env import cli as env_cli
from zata_ops.logs import cli as logs_cli
from zata_ops.observability import cli as observability_cli
from zata_ops.tunnel import cli as tunnel_cli

app: typer.Typer = typer.Typer(
    name="zata-ops",
    help=(
        "Operations toolkit for Zata downstream projects. "
        "Provides db backup/restore, S3 diagnostics, VPS provisioning, "
        "log inspection, and a terminal dashboard."
    ),
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(
    db_cli.app, name="db", help="Database backup, restore, list, migrate, check."
)
app.add_typer(env_cli.app, name="env", help="VPS environment provisioning and fixes.")
app.add_typer(logs_cli.app, name="logs", help="Tail and search container/system logs.")
app.add_typer(
    observability_cli.app, name="dashboard", help="Terminal status dashboard."
)
app.add_typer(
    tunnel_cli.app,
    name="tunnel",
    help=("SSH 端口转发(local 对应 ssh -L,remote 对应 ssh -R)," "前台常驻或后台守护。"),
)


def _print_version_and_exit(value: bool) -> None:
    """Eager ``--version`` callback used by Typer."""
    if value:
        typer.echo(f"zata-ops {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        is_eager=True,
        callback=_print_version_and_exit,
        help="Show the installed zata-ops version and exit.",
    ),
) -> None:
    """Root callback that handles ``--version``.

    Args:
        version: Triggered by Typer when ``--version`` is passed.
    """
    # The callback exists so that ``--version`` is parsed before any
    # subcommand requires its own arguments. No state to set up here.
    return None


if __name__ == "__main__":  # pragma: no cover
    app()
