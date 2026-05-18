"""Private render helpers for `apm mcp show`."""


def _get_server_version(server_info: dict) -> str:
    """Extract the version string from server info, trying multiple field paths."""
    if "version_detail" in server_info:
        return server_info["version_detail"].get("version", "Unknown")
    if "version" in server_info:
        return server_info["version"]
    return "Unknown"


def _collect_deployment_types(remotes: list, packages: list) -> list:
    """Build deployment-type labels for the server info table."""
    deployment_info = []
    for remote in remotes:
        if remote.get("transport_type", "unknown") == "sse":
            deployment_info.append(" Remote SSE Endpoint")
    if packages:
        deployment_info.append(" Local Package")
    return deployment_info


def _render_remotes_table(console, remotes: list, name: str) -> None:
    """Print the remote endpoints table to *console*."""
    from rich.table import Table

    remote_table = Table(
        title=" Remote Endpoints",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    remote_table.add_column("Type", style="yellow", width=10)
    remote_table.add_column("URL", style="white", min_width=40)
    remote_table.add_column("Features", style="cyan", min_width=20)

    is_github = "github" in name.lower()
    for remote in remotes:
        transport_type = remote.get("transport_type", "unknown")
        url = remote.get("url", "unknown")
        features = "No toolset customization" if is_github else "Hosted by provider"
        remote_table.add_row(transport_type.upper(), url, features)

    console.print(remote_table)


def _render_packages_table(console, packages: list, name: str) -> None:
    """Print the local packages table to *console*."""
    from rich.table import Table

    pkg_table = Table(
        title=" Local Packages",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    pkg_table.add_column("Registry", style="yellow", width=10)
    pkg_table.add_column("Package", style="white", min_width=25)
    pkg_table.add_column("Runtime", style="cyan", width=8, justify="center")
    pkg_table.add_column("Features", style="green", min_width=20)

    is_github = "github" in name.lower()
    for pkg in packages:
        registry_name = pkg.get("registry_name", "unknown")
        pkg_name = pkg.get("name", "unknown")
        runtime_hint = pkg.get("runtime_hint", " --")
        features = "Supports GITHUB_TOOLSETS" if is_github else "Full configuration control"
        if len(pkg_name) > 25:
            pkg_name = pkg_name[:22] + "..."
        pkg_table.add_row(registry_name, pkg_name, runtime_hint, features)

    console.print(pkg_table)


def _render_install_table(console, install_name: str) -> None:
    """Print the installation guide table to *console*."""
    from rich.table import Table

    install_table = Table(
        title="* Installation Guide",
        show_header=True,
        header_style="bold cyan",
        border_style="green",
    )
    install_table.add_column("Step", style="bold white", width=5)
    install_table.add_column("Action", style="white", min_width=30)
    install_table.add_column("Command/Config", style="cyan", min_width=25)

    install_table.add_row(
        "1",
        "Add to apm.yml dependencies",
        f"[yellow]mcp:[/yellow] [cyan]- {install_name}[/cyan]",
    )
    install_table.add_row("2", "Install dependencies", "[bold cyan]apm install[/bold cyan]")
    install_table.add_row(
        "3",
        "Direct install (coming soon)",
        f"[bold cyan]apm install {install_name}[/bold cyan]",
    )

    console.print(install_table)
