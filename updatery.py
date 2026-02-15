"""Package Update Monitor - A TUI tool for managing Windows package updates."""

import asyncio
import ctypes
import json
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    RichLog,
    SelectionList,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection
from textual.worker import Worker, WorkerState


@dataclass
class PackageInfo:
    """Represents a package with update information."""

    name: str
    package_id: str
    current_version: str
    available_version: str


class PackageManager(ABC):
    """Abstract base class for package managers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of the package manager."""
        pass

    @abstractmethod
    def is_installed(self) -> bool:
        """Check if the package manager is installed."""
        pass

    @abstractmethod
    def get_updatable_packages(self) -> list[PackageInfo]:
        """Get list of packages that can be updated."""
        pass

    @abstractmethod
    async def upgrade_package(self, package_id: str) -> tuple[int, str]:
        """Upgrade a single package. Returns (exit_code, message)."""
        pass

    @abstractmethod
    def get_exit_code_message(self, code: int) -> str:
        """Get human-readable message for exit code."""
        pass


class WingetManager(PackageManager):
    """Manager for winget packages."""

    @property
    def name(self) -> str:
        return "winget"

    def is_installed(self) -> bool:
        """Check if winget is available."""
        return shutil.which("winget") is not None

    def _parse_winget_list(self, output: str) -> list[dict[str, str]]:
        """Parse winget list output into a list of package dicts."""
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

    def get_updatable_packages(self) -> list[PackageInfo]:
        """Run winget list and return packages with available updates."""
        try:
            result = subprocess.run(
                ["winget", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("DEBUG: winget list timed out")
            return []
        except Exception as e:
            print(f"DEBUG: winget list error: {e}")
            return []

        output = result.stdout
        packages = self._parse_winget_list(output)

        return [
            PackageInfo(
                name=p.get("Name", "?"),
                package_id=p.get("Id", "?"),
                current_version=p.get("Version", "?"),
                available_version=p.get("Available", "?"),
            )
            for p in packages
            if p.get("Source") == "winget" and p.get("Available", "").strip()
        ]

    async def upgrade_package(self, package_id: str) -> tuple[int, str]:
        """Upgrade a winget package."""
        proc = await asyncio.create_subprocess_exec(
            "winget",
            "upgrade",
            "--id",
            package_id,
            "--silent",
            "--disable-interactivity",
            "--accept-source-agreements",
            "--accept-package-agreements",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines = []
        if proc.stdout is not None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line.decode("utf-8", errors="replace").rstrip())

        await proc.wait()
        return proc.returncode or 0, "\n".join(output_lines)

    def get_exit_code_message(self, code: int) -> str:
        """Get human-readable message for exit code."""
        codes = self._get_exit_codes()
        return codes.get(code, f"Unknown error (code {code})")

    def _get_exit_codes(self) -> dict[int, str]:
        """Build exit code map with both signed and unsigned variants."""

        def hex_codes(*pairs: tuple[int, str]) -> dict[int, str]:
            result: dict[int, str] = {}
            for code, msg in pairs:
                result[code] = msg
                # Python subprocess returns unsigned on Windows; add signed variant
                if code > 0x7FFFFFFF:
                    result[code - 0x100000000] = msg
            return result

        return {
            3: "Reboot required to complete",
            5: "Access denied — needs admin elevation",
            **hex_codes(
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


class NpmManager(PackageManager):
    """Manager for npm global packages."""

    @property
    def name(self) -> str:
        return "npm"

    def _get_npm_path(self) -> str:
        """Get the full path to npm executable."""
        npm_path = shutil.which("npm")
        return npm_path or "npm"

    def is_installed(self) -> bool:
        """Check if npm is available."""
        return shutil.which("npm") is not None

    def get_updatable_packages(self) -> list[PackageInfo]:
        """Get list of outdated npm global packages."""
        npm_path = self._get_npm_path()
        try:
            result = subprocess.run(
                [npm_path, "outdated", "-g", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return []
        except Exception as e:
            return []

        if result.returncode not in (0, 1):  # 1 means outdated packages found
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        packages = []
        for package_name, info in data.items():
            if isinstance(info, dict):
                current = info.get("current", "?")
                latest = info.get("latest", "?")
                if current and latest and current != latest:
                    packages.append(
                        PackageInfo(
                            name=package_name,
                            package_id=package_name,
                            current_version=current,
                            available_version=latest,
                        )
                    )

        return packages

    async def upgrade_package(self, package_id: str) -> tuple[int, str]:
        """Upgrade an npm global package."""
        npm_path = self._get_npm_path()
        proc = await asyncio.create_subprocess_exec(
            npm_path,
            "update",
            "-g",
            package_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines = []
        if proc.stdout is not None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line.decode("utf-8", errors="replace").rstrip())

        await proc.wait()
        return proc.returncode or 0, "\n".join(output_lines)

    def get_exit_code_message(self, code: int) -> str:
        """Get human-readable message for exit code."""
        npm_codes = {
            0: "Success",
            1: "General error",
            127: "Command not found",
        }
        return npm_codes.get(code, f"npm error (code {code})")


class PackageManagerWidget(Vertical):
    """Widget for managing packages from a specific package manager."""

    CSS = """
    PackageManagerWidget {
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
    #not-installed {
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    """

    def __init__(self, manager: PackageManager, **kwargs) -> None:
        super().__init__(**kwargs)
        self.manager = manager
        self._packages: list[PackageInfo] = []
        self._is_loading = False

    def compose(self) -> ComposeResult:
        if not self.manager.is_installed():
            yield Label(
                f"[bold yellow]{self.manager.name} is not installed.\n\n"
                f"Install {self.manager.name} to use this tab.[/bold yellow]",
                id="not-installed",
            )
            return

        yield SelectionList[str](id="pkg-list")
        with Horizontal(id="buttons"):
            yield Button("Refresh (r)", id="btn-refresh", variant="default")
            yield Button("Upgrade Selected (u)", id="btn-upgrade", variant="success")
            yield Button("Select All (a)", id="btn-select-all", variant="primary")
        yield RichLog(id="log", highlight=True, markup=True)

    def on_mount(self) -> None:
        if self.manager.is_installed():
            # Delay initial scan slightly to ensure widget is fully mounted
            self.set_timer(0.1, self.refresh_packages)

    def refresh_packages(self) -> None:
        """Refresh the package list."""
        if self._is_loading:
            return

        log = self.query_one("#log", RichLog)
        log.write(f"[bold]Scanning for {self.manager.name} updates...[/bold]")
        self._is_loading = True
        self.run_worker(self._load_packages, name="refresh", exclusive=True)

    async def _load_packages(self) -> list[PackageInfo]:
        try:
            return await asyncio.to_thread(self.manager.get_updatable_packages)
        except Exception as e:
            self.app.call_later(
                lambda: self._show_error(f"Error loading packages: {e}")
            )
            return []

    def _show_error(self, message: str) -> None:
        """Show error message in the log."""
        try:
            log = self.query_one("#log", RichLog)
            log.write(f"[bold red]{message}[/bold red]")
        except Exception:
            pass

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "refresh":
            self._is_loading = False
            if event.state == WorkerState.SUCCESS:
                packages = event.worker.result
                self._packages = packages
                self._populate_list(packages)
            elif event.state == WorkerState.ERROR:
                try:
                    log = self.query_one("#log", RichLog)
                    error_msg = str(event.worker.error)
                    log.write(f"[bold red]Error scanning: {error_msg}[/bold red]")
                except Exception as e:
                    self.log.error(f"Failed to display error: {e}")

    def _populate_list(self, packages: list[PackageInfo]) -> None:
        try:
            sel = self.query_one("#pkg-list", SelectionList)
            log = self.query_one("#log", RichLog)
            sel.clear_options()
            for p in packages:
                label = (
                    f"{p.name}  [dim]{p.package_id}[/dim]  "
                    f"{p.current_version} → [bold green]{p.available_version}[/bold green]"
                )
                sel.add_option(Selection(label, p.package_id, False))
            log.write(
                f"Found [bold]{len(packages)}[/bold] {self.manager.name} package(s) "
                f"with updates."
            )
        except Exception as e:
            self.log.error(f"Error populating list: {e}")

    def select_all(self) -> None:
        """Select all packages in the list."""
        if not self.manager.is_installed():
            return
        sel = self.query_one("#pkg-list", SelectionList)
        sel.select_all()

    def upgrade_selected(self) -> None:
        """Upgrade selected packages."""
        if not self.manager.is_installed():
            return

        sel = self.query_one("#pkg-list", SelectionList)
        selected = list(sel.selected)
        if not selected:
            log = self.query_one("#log", RichLog)
            log.write("[yellow]No packages selected.[/yellow]")
            return
        self.run_worker(self._run_upgrades(selected), name="upgrade", exclusive=True)

    async def _run_upgrades(self, package_ids: list[str]) -> None:
        log = self.query_one("#log", RichLog)
        log.write(
            f"[bold]Upgrading {len(package_ids)} {self.manager.name} package(s)..."
            f"[/bold]"
        )
        for pkg_id in package_ids:
            log.write(
                f"\n[bold cyan]>>> {self.manager.name} upgrade {pkg_id}[/bold cyan]"
            )
            try:
                rc, output = await self.manager.upgrade_package(pkg_id)
                if output:
                    for line in output.split("\n"):
                        if line.strip():
                            log.write(line)
                if rc == 0:
                    log.write(f"[green]Successfully upgraded {pkg_id}[/green]")
                    # Remove from the list since it's been upgraded
                    self._packages = [
                        p for p in self._packages if p.package_id != pkg_id
                    ]
                    self._populate_list(self._packages)
                else:
                    msg = self.manager.get_exit_code_message(rc)
                    log.write(f"[red]{pkg_id}: {msg}[/red]")
            except Exception as e:
                log.write(f"[bold red]Error upgrading {pkg_id}: {e}[/bold red]")
        log.write("[bold]All upgrades complete.[/bold]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self.manager.is_installed():
            return

        if event.button.id == "btn-refresh":
            self.refresh_packages()
        elif event.button.id == "btn-upgrade":
            self.upgrade_selected()
        elif event.button.id == "btn-select-all":
            self.select_all()


class UpdateryApp(App):
    """TUI app for monitoring and applying package updates."""

    TITLE = "Package Update Monitor"

    CSS = """
    #tab-content {
        height: 1fr;
    }
    TabbedContent {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("u", "upgrade", "Upgrade Selected"),
        Binding("a", "select_all", "Select All"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.winget_manager = WingetManager()
        self.npm_manager = NpmManager()

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tab-content"):
            with TabPane("winget", id="winget-tab"):
                yield PackageManagerWidget(self.winget_manager)
            with TabPane("npm", id="npm-tab"):
                yield PackageManagerWidget(self.npm_manager)
        yield Footer()

    def on_mount(self) -> None:
        # Check if running as admin for winget
        if not is_admin():
            winget_widget = self.query_one(
                "#winget-tab PackageManagerWidget", PackageManagerWidget
            )
            if winget_widget.manager.is_installed():
                log = winget_widget.query_one("#log", RichLog)
                log.write(
                    "[bold yellow]Warning: Not running as admin. "
                    "Some winget upgrades may fail or show UAC prompts. "
                    "Re-run as Administrator for best results.[/bold yellow]"
                )

    def action_refresh(self) -> None:
        """Refresh the current tab's package list."""
        active_widget = self._get_active_widget()
        if active_widget:
            active_widget.refresh_packages()

    def action_select_all(self) -> None:
        """Select all packages in the current tab."""
        active_widget = self._get_active_widget()
        if active_widget:
            active_widget.select_all()

    def action_upgrade(self) -> None:
        """Upgrade selected packages in the current tab."""
        active_widget = self._get_active_widget()
        if active_widget:
            active_widget.upgrade_selected()

    def _get_active_widget(self) -> Optional[PackageManagerWidget]:
        """Get the currently active PackageManagerWidget."""
        tab_content = self.query_one("#tab-content", TabbedContent)
        active_tab = tab_content.active

        if active_tab == "winget-tab":
            return self.query_one(
                "#winget-tab PackageManagerWidget", PackageManagerWidget
            )
        elif active_tab == "npm-tab":
            return self.query_one("#npm-tab PackageManagerWidget", PackageManagerWidget)
        return None


def is_admin() -> bool:
    """Check if the current process has admin privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


if __name__ == "__main__":
    if not is_admin():
        # Attempt to relaunch as admin
        try:
            # Build command line properly
            script_path = sys.argv[0]
            args = sys.argv[1:]

            # Try Windows Terminal first
            wt = shutil.which("wt")
            if wt:
                # Quote the wt path if it has spaces
                wt_quoted = f'"{wt}"' if " " in wt else wt
                # Build command: wt -- python script args
                cmd = f'-- "{sys.executable}" "{script_path}"'
                if args:
                    cmd += " " + " ".join(f'"{a}"' for a in args)
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", wt, cmd, None, 1
                )
            else:
                # Fallback: elevate python directly
                params = f'"{script_path}"'
                if args:
                    params += " " + " ".join(f'"{a}"' for a in args)
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, params, None, 1
                )

            if ret > 32:  # Success
                sys.exit(0)
            # If ret <= 32, elevation was cancelled or failed, continue without admin
        except Exception as e:
            # Elevation failed, continue without admin
            pass

    UpdateryApp().run()
