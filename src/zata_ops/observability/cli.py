"""Typer commands for ``zata-ops dashboard``: terminal status overview."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    no_args_is_help=False, add_completion=False, invoke_without_command=True
)
console = Console()


def _mock_status_provider(project: str) -> dict[str, Any]:
    """Return a deterministic mock status snapshot for smoke validation.

    Args:
        project: Project slug to label the snapshot with.

    Returns:
        Snapshot dictionary used to render the dashboard.
    """
    return {
        "project": project,
        "services": [
            {"name": f"{project}-backend", "status": "running", "uptime": "12h"},
            {"name": f"{project}-frontend", "status": "running", "uptime": "12h"},
            {"name": f"{project}-db", "status": "running", "uptime": "12h"},
        ],
        "last_backup": {
            "timestamp": "2026-06-07_180000",
            "type": "full",
            "size_mb": 42.0,
        },
        "resources": {"cpu_percent": 12.3, "memory_mb": 512},
    }


def _render_dashboard(status_snapshot: dict[str, Any]) -> None:
    """Render the dashboard snapshot using rich primitives."""
    console.print(
        Panel.fit(
            f"[bold cyan]zata-ops dashboard[/bold cyan]\n"
            f"project: [yellow]{status_snapshot['project']}[/yellow]"
        )
    )

    services_table = Table(title="Services", expand=False)
    services_table.add_column("Name")
    services_table.add_column("Status")
    services_table.add_column("Uptime")
    for service_entry in status_snapshot.get("services", []):
        services_table.add_row(
            service_entry["name"], service_entry["status"], service_entry["uptime"]
        )
    console.print(services_table)

    last_backup_entry = status_snapshot.get("last_backup")
    if last_backup_entry:
        console.print(
            Panel(
                f"timestamp: {last_backup_entry['timestamp']}\n"
                f"type: {last_backup_entry['type']}\n"
                f"size: {last_backup_entry['size_mb']:.1f} MB",
                title="Last backup",
            )
        )

    resources_entry = status_snapshot.get("resources", {})
    if resources_entry:
        console.print(
            Panel(
                f"cpu: {resources_entry.get('cpu_percent', 0):.1f}%\n"
                f"memory: {resources_entry.get('memory_mb', 0)} MB",
                title="Resources",
            )
        )


@app.callback(invoke_without_command=True)
def dashboard_command(
    project: str = typer.Option("app", help="Project slug to show in the dashboard."),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Render with a deterministic mock provider for smoke tests.",
    ),
) -> None:
    """Render the terminal status dashboard.

    Without ``--mock`` the dashboard placeholder shows the same layout
    populated with a mock provider — real status providers (compose, systemd,
    backup history) plug in via :func:`_render_dashboard` once implemented in
    a future release.
    """
    status_snapshot = _mock_status_provider(project)
    _render_dashboard(status_snapshot)
    if not mock:
        console.print(
            "[dim]note: real status providers are not yet implemented; "
            "rendering mock snapshot.[/dim]"
        )
