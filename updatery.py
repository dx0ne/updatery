"""Winget Update Monitor - A TUI tool for managing Windows package updates."""

import asyncio
import ctypes
import subprocess
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, RichLog, SelectionList
from textual.widgets.selection_list import Selection
from textual.worker import Worker, WorkerState


def parse_winget_list(output: str) -> list[dict[str, str]]:
    """Parse winget list output into a list of package dicts.

    The separator line may be one continuous block of dashes, so we derive
    column positions from the header keywords instead.
    """
    lines = output.splitlines()

    # Find the header line containing known column names
    header_idx = None
    for i, line in enumerate(lines):
        if "Name" in line and "Id" in line and "Version" in line:
            header_idx = i
            break

    if header_idx is None:
        return []

    header_line = lines[header_idx]

    # Find separator line (all dashes) right after header
    sep_idx = header_idx + 1
    if sep_idx >= len(lines) or not lines[sep_idx].strip().replace("-", "") == "":
        return []

    # Determine column start positions from the header text
    col_names = ["Name", "Id", "Version", "Available", "Source"]
    col_starts: list[tuple[str, int]] = []
    for name in col_names:
        pos = header_line.find(name)
        if pos >= 0:
            col_starts.append((name, pos))

    if len(col_starts) < 3:
        return []

    # Sort by position
    col_starts.sort(key=lambda x: x[1])

    packages: list[dict[str, str]] = []
    for line in lines[sep_idx + 1 :]:
        if not line.strip():
            continue
        values: dict[str, str] = {}
        for j, (name, start) in enumerate(col_starts):
            if j + 1 < len(col_starts):
                end = col_starts[j + 1][1]
            else:
                end = len(line)
            val = line[start:end].strip() if start < len(line) else ""
            values[name] = val
        packages.append(values)

    return packages


def get_updatable_packages() -> list[dict[str, str]]:
    """Run winget list and return packages with available updates from winget source."""
    result = subprocess.run(
        ["winget", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = result.stdout
    packages = parse_winget_list(output)
    return [
        p
        for p in packages
        if p.get("Source") == "winget" and p.get("Available", "").strip()
    ]


def _hex_codes(*pairs: tuple[int, str]) -> dict[int, str]:
    """Build exit code map with both signed and unsigned variants."""
    result: dict[int, str] = {}
    for code, msg in pairs:
        result[code] = msg
        # Python subprocess returns unsigned on Windows; add signed variant
        if code > 0x7FFFFFFF:
            result[code - 0x100000000] = msg
    return result


WINGET_EXIT_CODES = {
    3: "Reboot required to complete",
    5: "Access denied — needs admin elevation",
    **_hex_codes(
        (0x8A150006, "ShellExecute install failed"),
        (0x8A150008, "Downloading installer failed"),
        (0x8A150010, "No applicable installer for this system"),
        (0x8A150011, "Installer hash mismatch"),
        (0x8A150019, "Requires administrator privileges"),
        (0x8A15002B, "No applicable update found"),
        (0x8A15004F, "Upgrade version is not newer than installed"),
        (0x8A150061, "Package is already installed"),
        (0x8A150101, "App is running — close it and retry"),
        (0x8A150102, "Another installation in progress — try later"),
        (0x8A150103, "File in use — close the app and retry"),
        (0x8A150105, "Not enough disk space"),
        (0x8A150108, "Installer error — contact support"),
        (0x8A150109, "Restart PC to finish installation"),
        (0x8A15010A, "Installation failed — restart PC and retry"),
        (0x8A15010C, "Installation was cancelled"),
        (0x8A15010D, "Another version already installed"),
        (0x8A15010E, "A higher version is already installed"),
        (0x8A15010F, "Blocked by organization policy"),
    ),
}


def is_admin() -> bool:
    """Check if the current process has admin privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


class WingetMonitor(App):
    """TUI app for monitoring and applying winget updates."""

    TITLE = "Winget Update Monitor"

    CSS = """
    #main {
        height: 1fr;
    }
    #pkg-list {
        height: 1fr;
        border: solid $accent;
    }
    #buttons {
        height: auto;
        padding: 1 0;
        align: center middle;
    }
    #buttons Button {
        margin: 0 2;
    }
    #log {
        height: 12;
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("u", "upgrade", "Upgrade Selected"),
        Binding("a", "select_all", "Select All"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            yield SelectionList[str](id="pkg-list")
            with Horizontal(id="buttons"):
                yield Button("Refresh (r)", id="btn-refresh", variant="default")
                yield Button("Upgrade Selected (u)", id="btn-upgrade", variant="success")
                yield Button("Select All (a)", id="btn-select-all", variant="primary")
            yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        if not is_admin():
            log = self.query_one("#log", RichLog)
            log.write(
                "[bold yellow]Warning: Not running as admin. "
                "Some upgrades may fail or show UAC prompts. "
                "Re-run as Administrator for best results.[/bold yellow]"
            )
        self.action_refresh()

    def action_refresh(self) -> None:
        """Refresh the package list."""
        log = self.query_one("#log", RichLog)
        log.write("[bold]Scanning for updates...[/bold]")
        self.run_worker(self._load_packages, name="refresh", exclusive=True)

    async def _load_packages(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(get_updatable_packages)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "refresh":
            if event.state == WorkerState.SUCCESS:
                packages = event.worker.result
                self._populate_list(packages)
            elif event.state == WorkerState.ERROR:
                log = self.query_one("#log", RichLog)
                log.write(f"[bold red]Error scanning: {event.worker.error}[/bold red]")

    def _populate_list(self, packages: list[dict[str, str]]) -> None:
        sel = self.query_one("#pkg-list", SelectionList)
        log = self.query_one("#log", RichLog)
        sel.clear_options()
        for p in packages:
            name = p.get("Name", "?")
            pkg_id = p.get("Id", "?")
            ver = p.get("Version", "?")
            avail = p.get("Available", "?")
            label = f"{name}  [dim]{pkg_id}[/dim]  {ver} → [bold green]{avail}[/bold green]"
            sel.add_option(Selection(label, pkg_id, False))
        log.write(f"Found [bold]{len(packages)}[/bold] package(s) with updates.")

    def action_select_all(self) -> None:
        sel = self.query_one("#pkg-list", SelectionList)
        sel.select_all()

    def action_upgrade(self) -> None:
        sel = self.query_one("#pkg-list", SelectionList)
        selected = list(sel.selected)
        if not selected:
            log = self.query_one("#log", RichLog)
            log.write("[yellow]No packages selected.[/yellow]")
            return
        self.run_worker(
            self._run_upgrades(selected), name="upgrade", exclusive=True
        )

    async def _run_upgrades(self, package_ids: list[str]) -> None:
        log = self.query_one("#log", RichLog)
        log.write(f"[bold]Upgrading {len(package_ids)} package(s)...[/bold]")
        for pkg_id in package_ids:
            log.write(f"\n[bold cyan]>>> winget upgrade --id {pkg_id}[/bold cyan]")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "winget",
                    "upgrade",
                    "--id",
                    pkg_id,
                    "--silent",
                    "--disable-interactivity",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    log.write(line.decode("utf-8", errors="replace").rstrip())
                await proc.wait()
                rc = proc.returncode
                if rc == 0:
                    log.write(f"[green]Successfully upgraded {pkg_id}[/green]")
                else:
                    msg = WINGET_EXIT_CODES.get(rc, f"Unknown error (code {rc})")
                    log.write(f"[red]{pkg_id}: {msg}[/red]")
            except Exception as e:
                log.write(f"[bold red]Error upgrading {pkg_id}: {e}[/bold red]")
        log.write("[bold]All upgrades complete.[/bold]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.action_refresh()
        elif event.button.id == "btn-upgrade":
            self.action_upgrade()
        elif event.button.id == "btn-select-all":
            self.action_select_all()


if __name__ == "__main__":
    import shutil

    if not is_admin():
        # Attempt to relaunch as admin inside Windows Terminal
        try:
            script = " ".join(f'"{a}"' for a in sys.argv)
            cmd = f'"{sys.executable}" {script}'
            wt = shutil.which("wt") or shutil.which("wt", path="C:\\Program Files\\WindowsApps")
            if wt:
                # Launch Windows Terminal running the command elevated
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", wt, f"-- {cmd}", None, 1
                )
            else:
                # Fallback: launch python directly (will use conhost)
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, script, None, 1
                )
            if ret > 32:  # Success — the elevated process is launching
                sys.exit(0)
        except Exception:
            pass
        # If elevation failed or was declined, run anyway
    WingetMonitor().run()
