"""
Terminal User Interface for PowerBI CLI using Textual

This module provides an interactive TUI for all PowerBI CLI functionalities,
making it easy to manage authentication, workspaces, apps, reports, and users.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from slugify import slugify
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

import pbi_cli.powerbi.admin as powerbi_admin
import pbi_cli.powerbi.app as powerbi_app
from pbi_cli.cache import CacheManager
from pbi_cli.cli import (
    _delete_credential,
    _get_credential,
    _load_profiles,
    _save_profiles,
    _set_credential,
    load_auth,
)
from pbi_cli.config import PBIConfig

# Configuration constants
CACHE_EXPIRY_HOURS = 1
DEFAULT_WORKSPACE_LIMIT = 1000
MAX_ID_LENGTH = 20
MAX_TEXT_LENGTH = 50


class MainMenuScreen(Screen):
    """Main menu screen for the PowerBI CLI TUI"""

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.cache_manager: Optional[CacheManager] = None
        self.pbi_config: Optional[PBIConfig] = None

    @staticmethod
    def _truncate_id(id_str: str, max_len: int = 36) -> str:
        """Truncate ID string to specified length."""
        if not id_str or len(id_str) <= max_len:
            return id_str
        return id_str[:max_len]

    @staticmethod
    def _truncate_text(text: str, max_len: int = 50) -> str:
        """Truncate text with ellipsis if needed."""
        if not text or len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def compose(self) -> ComposeResult:
        """Create child widgets for the main menu."""
        yield Header()

        with Vertical(id="main-container"):
            # Top selectors bar - FUNCTIONAL selectors
            with Horizontal(id="filter-bar"):
                yield Select(
                    options=[("Loading...", "loading")],
                    prompt="Profile",
                    id="profile-selector",
                    classes="filter-selector",
                    allow_blank=False,
                )
                yield Input(
                    placeholder="Output Folder (leave empty for default)",
                    id="output-folder-input",
                    classes="filter-input",
                )
                yield Button("Set Folder", id="btn-set-folder", variant="primary")

            # Actions bar - Combined Auth/Config button, and data buttons
            with Horizontal(id="actions-bar", classes="action-buttons-bar"):
                yield Button(
                    "[1] Profiles & Config", id="btn-profiles-config", variant="default"
                )
                yield Button("[2] Workspaces", id="btn-workspaces", variant="default")
                yield Button("[3] Apps", id="btn-apps", variant="default")
                yield Button("[4] Reports", id="btn-reports", variant="default")
                yield Button("[5] Users", id="btn-users", variant="default")

            # Main content area - results pane
            with Container(id="results-container"):
                yield Static(
                    "PowerBI CLI - Welcome", classes="panel-header", id="results-header"
                )
                yield ScrollableContainer(
                    Static("", id="results-content"), id="results-scroll"
                )

        yield Footer()

    def on_mount(self) -> None:
        """Initialize the screen with data"""
        self.load_config()
        self.load_profiles()
        self.show_welcome_message()

    def load_config(self) -> None:
        """Load configuration"""
        try:
            self.pbi_config = PBIConfig()

            # Initialize cache manager if configured
            if self.pbi_config.cache_folder and self.pbi_config.cache_enabled:
                self.cache_manager = CacheManager(
                    cache_folder=self.pbi_config.cache_folder
                )

            # Update output folder input
            output_input = self.query_one("#output-folder-input", Input)
            if self.pbi_config.default_output_folder:
                output_input.value = self.pbi_config.default_output_folder

        except Exception as e:
            self.show_error(f"Error loading config: {str(e)}")

    def load_profiles(self) -> None:
        """Load profiles and update profile selector"""
        try:
            profiles_data = _load_profiles()
            profiles = profiles_data.get("profiles", {})
            active_profile = profiles_data.get("active_profile") or "None"

            profile_selector = self.query_one("#profile-selector", Select)
            
            if not profiles:
                profile_selector.set_options([("No profiles", "none")])
            else:
                options = [
                    (
                        f"{name} {'(active)' if name == active_profile else ''}",
                        name,
                    )
                    for name in profiles.keys()
                ]
                profile_selector.set_options(options)
                
                # Set the active profile as selected
                if active_profile and active_profile in profiles:
                    profile_selector.value = active_profile

        except Exception as e:
            self.show_error(f"Error loading profiles: {str(e)}")

    @on(Select.Changed, "#profile-selector")
    def on_profile_changed(self, event: Select.Changed) -> None:
        """Handle profile selection change - SWITCH the active profile"""
        if event.value and event.value not in ["loading", "none"]:
            try:
                profiles_data = _load_profiles()
                profiles_data["active_profile"] = event.value
                _save_profiles(profiles_data)
                
                # Reload to update display
                self.load_profiles()
                self.show_info(f"Switched to profile: {event.value}")
            except Exception as e:
                self.show_error(f"Error switching profile: {str(e)}")

    @on(Button.Pressed, "#btn-set-folder")
    def on_set_folder(self) -> None:
        """Handle output folder change"""
        try:
            folder_input = self.query_one("#output-folder-input", Input)
            folder_path = folder_input.value.strip()

            if not self.pbi_config:
                self.pbi_config = PBIConfig()

            if folder_path:
                # Set the folder
                self.pbi_config.default_output_folder = folder_path
                self.show_info(f"Output folder set to: {folder_path}")
            else:
                # Clear the folder
                self.pbi_config.default_output_folder = None
                self.show_info("Output folder cleared (using default)")
            
            # Reload config
            self.load_config()

        except Exception as e:
            self.show_error(f"Error setting output folder: {str(e)}")

    def show_welcome_message(self) -> None:
        """Display welcome message in results pane."""
        self.query_one("#results-header", Static).update("PowerBI CLI - Welcome")
        
        content = self.query_one("#results-content", Static)
        welcome_text = """
[bold cyan]Welcome to PowerBI CLI TUI[/bold cyan]

[yellow]Quick Actions:[/yellow]
• [1] Profiles & Config - Manage authentication profiles and settings
• [2] Workspaces - List and manage Power BI workspaces
• [3] Apps - View and interact with Power BI apps
• [4] Reports - Augment reports with user data
• [5] Users - Get user access information

[yellow]Top Controls:[/yellow]
• Profile Selector - Switch between authentication profiles
• Output Folder - Set default output folder for exports
• Set Folder - Apply output folder changes

[yellow]Keyboard Shortcuts:[/yellow]
• q - Quit application
• Ctrl+R - Refresh current view

[green]Ready to use![/green]
"""
        content.update(welcome_text)

    def show_info(self, message: str) -> None:
        """Display info message in results pane."""
        self.query_one("#results-header", Static).update("Information")
        content = self.query_one("#results-content", Static)
        content.update(f"[cyan]{message}[/cyan]")

    def show_error(self, message: str) -> None:
        """Display error in results pane."""
        self.query_one("#results-header", Static).update("Error")
        content = self.query_one("#results-content", Static)
        content.update(f"[red]{message}[/red]")

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()

    def action_refresh(self) -> None:
        """Refresh data"""
        self.load_config()
        self.load_profiles()
        self.show_info("Refreshed configuration and profiles")

    @on(Button.Pressed, "#btn-profiles-config")
    def on_profiles_config_button(self) -> None:
        """Show combined profiles and config view"""
        self.show_profiles_and_config()

    @on(Button.Pressed, "#btn-workspaces")
    def on_workspaces_button(self) -> None:
        self.app.push_screen(WorkspacesScreen())

    @on(Button.Pressed, "#btn-apps")
    def on_apps_button(self) -> None:
        self.app.push_screen(AppsScreen())

    @on(Button.Pressed, "#btn-reports")
    def on_reports_button(self) -> None:
        self.app.push_screen(ReportsScreen())

    @on(Button.Pressed, "#btn-users")
    def on_users_button(self) -> None:
        self.app.push_screen(UsersScreen())

    def show_profiles_and_config(self) -> None:
        """Display profiles and config in a combined view"""
        try:
            self.query_one("#results-header", Static).update(
                "Profiles & Configuration"
            )
            
            # Load data
            profiles_data = _load_profiles()
            profiles = profiles_data.get("profiles", {})
            active_profile = profiles_data.get("active_profile")
            pbi_config = PBIConfig()

            # Build combined view
            content_parts = []
            
            # Profiles section
            content_parts.append("[bold yellow]AUTHENTICATION PROFILES:[/bold yellow]\n")
            if not profiles:
                content_parts.append("[dim]No profiles found[/dim]\n")
            else:
                for profile_name in profiles.keys():
                    is_active = profile_name == active_profile
                    token_exists = _get_credential(profile_name) is not None
                    
                    status = "✓" if token_exists else "✗"
                    active_marker = " [green](ACTIVE)[/green]" if is_active else ""
                    token_status = "Token: Yes" if token_exists else "Token: No"
                    
                    content_parts.append(
                        f"  {status} [cyan]{profile_name}[/cyan]{active_marker} - {token_status}\n"
                    )
            
            # Configuration section
            content_parts.append("\n[bold yellow]CONFIGURATION SETTINGS:[/bold yellow]\n")
            content_parts.append(f"  Active Profile: [cyan]{active_profile or 'None'}[/cyan]\n")
            content_parts.append(
                f"  Output Folder: [cyan]{pbi_config.default_output_folder or 'Not set'}[/cyan]\n"
            )
            content_parts.append(f"  Config File: [dim]{pbi_config.config_file}[/dim]\n")
            content_parts.append(
                f"  Cache Folder: [cyan]{pbi_config.cache_folder or 'Not set'}[/cyan]\n"
            )
            content_parts.append(
                f"  Cache Enabled: [cyan]{'Yes' if pbi_config.cache_enabled else 'No'}[/cyan]\n"
            )
            
            # Actions section
            content_parts.append("\n[bold yellow]PROFILE ACTIONS:[/bold yellow]\n")
            content_parts.append("  • Use Profile Selector (top) to switch profiles\n")
            content_parts.append("  • Use 'pbi auth add-profile' CLI to add new profiles\n")
            content_parts.append("  • Use 'pbi auth delete-profile' CLI to remove profiles\n")
            
            content_parts.append("\n[bold yellow]CONFIG ACTIONS:[/bold yellow]\n")
            content_parts.append("  • Use Output Folder input (top) to change output folder\n")
            content_parts.append("  • Use 'pbi config' CLI for advanced settings\n")

            content = self.query_one("#results-content", Static)
            content.update("".join(content_parts))

        except Exception as e:
            self.show_error(f"Error loading profiles/config: {str(e)}")


class WorkspacesScreen(Screen):
    """Workspaces management screen with caching"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.workspaces_data: Optional[Dict] = None
        self.cache_manager: Optional[CacheManager] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="workspaces-container"):
            yield Static("Workspaces", classes="screen-title", id="workspaces-title")
            
            with Horizontal(id="workspaces-controls"):
                yield Button("Load Workspaces", id="btn-load", variant="primary")
                yield Button("Refresh (Force)", id="btn-refresh", variant="warning")
                yield Button("Back", id="btn-back", variant="default")
            
            yield Static("", id="workspaces-status")
            yield DataTable(id="workspaces-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize cache manager"""
        try:
            pbi_config = PBIConfig()
            if pbi_config.cache_folder and pbi_config.cache_enabled:
                self.cache_manager = CacheManager(cache_folder=pbi_config.cache_folder)
        except Exception as e:
            self.update_status(f"Cache initialization warning: {str(e)}", "yellow")

    def update_status(self, message: str, color: str = "cyan") -> None:
        """Update status message"""
        status = self.query_one("#workspaces-status", Static)
        status.update(f"[{color}]{message}[/{color}]")

    @staticmethod
    def _truncate_id(id_str: str, max_len: int = 36) -> str:
        """Truncate ID string to specified length."""
        if not id_str or len(id_str) <= max_len:
            return id_str
        return id_str[:max_len]

    def is_cache_valid(self, cache_key: str) -> bool:
        """Check if cache exists and is less than 1 hour old"""
        if not self.cache_manager:
            return False
        
        cached_data = self.cache_manager.load(cache_key, version="latest")
        if not cached_data:
            return False
        
        # Check cache age
        cached_at_str = cached_data.get("cached_at", "")
        if cached_at_str:
            try:
                cached_at = datetime.fromisoformat(cached_at_str)
                age = datetime.now() - cached_at
                return age < timedelta(hours=CACHE_EXPIRY_HOURS)
            except Exception:
                return False
        
        return False

    @work(thread=True, exclusive=True)
    def load_workspaces_worker(self, force_refresh: bool = False) -> None:
        """Worker to load workspaces with caching"""
        cache_key = "workspaces_admin"
        
        try:
            # Try cache first (unless force refresh)
            if not force_refresh and self.is_cache_valid(cache_key):
                cached_data = self.cache_manager.load(cache_key, version="latest")
                if cached_data:
                    self.workspaces_data = cached_data.get("data")
                    cache_time = cached_data.get("cached_at", "unknown")
                    self.app.call_from_thread(
                        self.update_status,
                        f"Loaded from cache ({cache_time})",
                        "green",
                    )
                    self.app.call_from_thread(self.display_workspaces)
                    return
            
            # Fetch from API
            self.app.call_from_thread(
                self.update_status, "Fetching from Power BI API...", "cyan"
            )
            
            auth = load_auth()
            workspaces_api = powerbi_admin.Workspaces(auth=auth, verify=False)
            result = workspaces_api(top=DEFAULT_WORKSPACE_LIMIT, expand=[], filter=None)
            
            self.workspaces_data = result
            
            # Save to cache
            if self.cache_manager:
                self.cache_manager.save(
                    cache_key,
                    result,
                    metadata={"source": "admin_api", "expand": []},
                )
            
            self.app.call_from_thread(
                self.update_status,
                f"Loaded {len(result.get('value', []))} workspaces from API",
                "green",
            )
            self.app.call_from_thread(self.display_workspaces)
            
        except Exception as e:
            self.app.call_from_thread(
                self.update_status, f"Error: {str(e)}", "red"
            )

    def display_workspaces(self) -> None:
        """Display workspaces in the table"""
        table = self.query_one("#workspaces-table", DataTable)
        table.clear(columns=True)
        
        if not self.workspaces_data or "value" not in self.workspaces_data:
            return
        
        workspaces = self.workspaces_data.get("value", [])
        
        if not workspaces:
            table.add_columns("Info")
            table.add_row("No workspaces found")
            return
        
        # Add columns
        table.add_columns("Name", "ID", "Type", "State", "Capacity")
        
        # Add rows
        for ws in workspaces:
            table.add_row(
                ws.get("name", "N/A"),
                self._truncate_id(ws.get("id", "N/A")),
                ws.get("type", "N/A"),
                ws.get("state", "N/A"),
                self._truncate_id(ws.get("capacityId", "N/A"), 20) if ws.get("capacityId") else "None",
            )

    @on(Button.Pressed, "#btn-load")
    def on_load(self) -> None:
        """Load workspaces (use cache if valid)"""
        self.update_status("Loading workspaces...", "cyan")
        self.load_workspaces_worker(force_refresh=False)

    @on(Button.Pressed, "#btn-refresh")
    def on_refresh(self) -> None:
        """Force refresh from API"""
        self.update_status("Refreshing from API...", "yellow")
        self.load_workspaces_worker(force_refresh=True)

    def action_refresh(self) -> None:
        """Keyboard shortcut for refresh"""
        self.on_refresh()

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class AppsScreen(Screen):
    """Apps management screen with caching"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.apps_data: Optional[Dict] = None
        self.cache_manager: Optional[CacheManager] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="apps-container"):
            yield Static("Apps", classes="screen-title", id="apps-title")
            
            with Horizontal(id="apps-controls"):
                yield Select(
                    options=[("User (My Apps)", "user"), ("Admin (All Apps)", "admin")],
                    prompt="Role",
                    id="role-selector",
                    value="user",
                )
                yield Button("Load Apps", id="btn-load", variant="primary")
                yield Button("Refresh (Force)", id="btn-refresh", variant="warning")
                yield Button("Back", id="btn-back", variant="default")
            
            yield Static("", id="apps-status")
            yield DataTable(id="apps-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize cache manager"""
        try:
            pbi_config = PBIConfig()
            if pbi_config.cache_folder and pbi_config.cache_enabled:
                self.cache_manager = CacheManager(cache_folder=pbi_config.cache_folder)
        except Exception as e:
            self.update_status(f"Cache initialization warning: {str(e)}", "yellow")

    def update_status(self, message: str, color: str = "cyan") -> None:
        """Update status message"""
        status = self.query_one("#apps-status", Static)
        status.update(f"[{color}]{message}[/{color}]")

    @staticmethod
    def _truncate_id(id_str: str, max_len: int = 36) -> str:
        """Truncate ID string to specified length."""
        if not id_str or len(id_str) <= max_len:
            return id_str
        return id_str[:max_len]

    @staticmethod
    def _truncate_text(text: str, max_len: int = 50) -> str:
        """Truncate text with ellipsis if needed."""
        if not text or len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def is_cache_valid(self, cache_key: str) -> bool:
        """Check if cache exists and is less than 1 hour old"""
        if not self.cache_manager:
            return False
        
        cached_data = self.cache_manager.load(cache_key, version="latest")
        if not cached_data:
            return False
        
        # Check cache age
        cached_at_str = cached_data.get("cached_at", "")
        if cached_at_str:
            try:
                cached_at = datetime.fromisoformat(cached_at_str)
                age = datetime.now() - cached_at
                return age < timedelta(hours=CACHE_EXPIRY_HOURS)
            except Exception:
                return False
        
        return False

    @work(thread=True, exclusive=True)
    def load_apps_worker(self, role: str, force_refresh: bool = False) -> None:
        """Worker to load apps with caching"""
        cache_key = f"apps_{role}"
        
        try:
            # Try cache first (unless force refresh)
            if not force_refresh and self.is_cache_valid(cache_key):
                cached_data = self.cache_manager.load(cache_key, version="latest")
                if cached_data:
                    self.apps_data = cached_data.get("data")
                    cache_time = cached_data.get("cached_at", "unknown")
                    self.app.call_from_thread(
                        self.update_status,
                        f"Loaded from cache ({cache_time}) as {role}",
                        "green",
                    )
                    self.app.call_from_thread(self.display_apps)
                    return
            
            # Fetch from API
            self.app.call_from_thread(
                self.update_status, f"Fetching from Power BI API as {role}...", "cyan"
            )
            
            auth = load_auth()
            
            # Use correct Apps class based on role
            if role == "user":
                apps_api = powerbi_app.Apps(auth=auth, verify=False)
            else:  # admin
                apps_api = powerbi_admin.Apps(auth=auth, verify=False)
            
            result = apps_api()
            
            self.apps_data = result
            
            # Save to cache
            if self.cache_manager:
                self.cache_manager.save(
                    cache_key, result, metadata={"role": role}
                )
            
            self.app.call_from_thread(
                self.update_status,
                f"Loaded {len(result.get('value', []))} apps from API ({role})",
                "green",
            )
            self.app.call_from_thread(self.display_apps)
            
        except Exception as e:
            self.app.call_from_thread(
                self.update_status, f"Error: {str(e)}", "red"
            )

    def display_apps(self) -> None:
        """Display apps in the table"""
        table = self.query_one("#apps-table", DataTable)
        table.clear(columns=True)
        
        if not self.apps_data or "value" not in self.apps_data:
            return
        
        apps = self.apps_data.get("value", [])
        
        if not apps:
            table.add_columns("Info")
            table.add_row("No apps found")
            return
        
        # Add columns
        table.add_columns("Name", "ID", "Description", "Published By")
        
        # Add rows
        for app in apps:
            desc = app.get("description", "") or ""
            
            table.add_row(
                app.get("name", "N/A"),
                self._truncate_id(app.get("id", "N/A")),
                self._truncate_text(desc),
                app.get("publishedBy", "N/A"),
            )

    @on(Button.Pressed, "#btn-load")
    def on_load(self) -> None:
        """Load apps (use cache if valid)"""
        role_selector = self.query_one("#role-selector", Select)
        role = role_selector.value or "user"
        self.update_status(f"Loading apps as {role}...", "cyan")
        self.load_apps_worker(role, force_refresh=False)

    @on(Button.Pressed, "#btn-refresh")
    def on_refresh(self) -> None:
        """Force refresh from API"""
        role_selector = self.query_one("#role-selector", Select)
        role = role_selector.value or "user"
        self.update_status(f"Refreshing from API as {role}...", "yellow")
        self.load_apps_worker(role, force_refresh=True)

    def action_refresh(self) -> None:
        """Keyboard shortcut for refresh"""
        self.on_refresh()

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class ReportsScreen(Screen):
    """Reports management screen - augment reports with user data"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="reports-container"):
            yield Static("Reports - Augment with User Data", classes="screen-title")
            
            yield Static(
                "[yellow]This feature augments Power BI Apps data with report user details[/yellow]",
                id="reports-info",
            )
            
            yield Label("Source File Path (JSON file from apps list):")
            yield Input(
                placeholder="/path/to/apps.json",
                id="source-file-input",
            )
            
            yield Label("Target Path (file or folder):")
            yield Input(
                placeholder="/path/to/output",
                id="target-path-input",
            )
            
            yield Label("File Type:")
            yield Select(
                options=[("JSON", "json"), ("Excel", "excel")],
                prompt="Select file type",
                id="file-type-selector",
                value="json",
            )
            
            with Horizontal(id="reports-controls"):
                yield Button("Augment Reports", id="btn-augment", variant="primary")
                yield Button("Back", id="btn-back", variant="default")
            
            yield Static("", id="reports-status")
            yield ScrollableContainer(Static("", id="reports-result"))
        yield Footer()

    def update_status(self, message: str, color: str = "cyan") -> None:
        """Update status message"""
        status = self.query_one("#reports-status", Static)
        status.update(f"[{color}]{message}[/{color}]")

    @on(Button.Pressed, "#btn-augment")
    def on_augment(self) -> None:
        """Augment reports with user data"""
        try:
            source_input = self.query_one("#source-file-input", Input)
            target_input = self.query_one("#target-path-input", Input)
            file_type_selector = self.query_one("#file-type-selector", Select)
            
            source_path = source_input.value.strip()
            target_path = target_input.value.strip()
            file_type = file_type_selector.value or "json"
            
            if not source_path:
                self.update_status("Please enter source file path", "yellow")
                return
            
            if not target_path:
                self.update_status("Please enter target path", "yellow")
                return
            
            source_file = Path(source_path).expanduser().absolute()
            target_file = Path(target_path).expanduser().absolute()
            
            if not source_file.exists():
                self.update_status(f"Source file not found: {source_file}", "red")
                return
            
            # This would require implementing the actual augmentation logic
            # For now, show what would be done
            result_text = f"""
[yellow]Report Augmentation Configuration:[/yellow]

Source File: [cyan]{source_file}[/cyan]
Target Path: [cyan]{target_file}[/cyan]
File Type: [cyan]{file_type}[/cyan]

[yellow]Note:[/yellow] This feature requires admin access to get report user details.

[yellow]To execute this operation:[/yellow]
Use the CLI command:
[cyan]pbi reports users --source {source_file} --target {target_file} --file-type {file_type}[/cyan]

This TUI provides the interface to configure parameters.
The actual processing is computationally intensive and better suited for CLI execution.
"""
            
            result_container = self.query_one("#reports-result", ScrollableContainer)
            result_static = result_container.query_one(Static)
            result_static.update(result_text)
            
            self.update_status("Configuration ready - use CLI to execute", "green")
            
        except Exception as e:
            self.update_status(f"Error: {str(e)}", "red")

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class UsersScreen(Screen):
    """Users management screen - get user access information"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.user_data: Optional[Dict] = None
        self.cache_manager: Optional[CacheManager] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="users-container"):
            yield Static("Users - Access Information", classes="screen-title")
            
            yield Static(
                "[yellow]Get user access information from Power BI (requires admin)[/yellow]",
                id="users-info",
            )
            
            yield Label("User ID (email address):")
            yield Input(
                placeholder="user@example.com",
                id="user-id-input",
            )
            
            yield Label("Target Folder (optional - leave empty to view only):")
            yield Input(
                placeholder="/path/to/output/folder",
                id="target-folder-input",
            )
            
            yield Label("File Types (if saving):")
            with Horizontal(id="file-types-container"):
                yield Select(
                    options=[
                        ("JSON", "json"),
                        ("Excel", "excel"),
                        ("CSV", "csv"),
                    ],
                    prompt="File type",
                    id="file-type-selector",
                    value="json",
                )
            
            yield Label("File Name (optional - derived from user-id if empty):")
            yield Input(
                placeholder="user-access",
                id="file-name-input",
            )
            
            with Horizontal(id="users-controls"):
                yield Button("Get User Access", id="btn-get-user", variant="primary")
                yield Button("Back", id="btn-back", variant="default")
            
            yield Static("", id="users-status")
            yield ScrollableContainer(Static("", id="users-result"))
        yield Footer()

    def on_mount(self) -> None:
        """Initialize cache manager"""
        try:
            pbi_config = PBIConfig()
            if pbi_config.cache_folder and pbi_config.cache_enabled:
                self.cache_manager = CacheManager(cache_folder=pbi_config.cache_folder)
        except Exception as e:
            self.update_status(f"Cache initialization warning: {str(e)}", "yellow")

    def update_status(self, message: str, color: str = "cyan") -> None:
        """Update status message"""
        status = self.query_one("#users-status", Static)
        status.update(f"[{color}]{message}[/{color}]")

    @work(thread=True, exclusive=True)
    def get_user_access_worker(self, user_id: str) -> None:
        """Worker to get user access information"""
        try:
            # Check cache first
            if self.cache_manager:
                cache_key = f"user_access_{slugify(user_id)}"
                cached_data = self.cache_manager.load(cache_key, version="latest")
                
                if cached_data:
                    cached_at_str = cached_data.get("cached_at", "")
                    try:
                        cached_at = datetime.fromisoformat(cached_at_str)
                        age = datetime.now() - cached_at
                        if age < timedelta(hours=CACHE_EXPIRY_HOURS):
                            self.user_data = cached_data.get("data")
                            cache_time = cached_data.get("cached_at", "unknown")
                            self.app.call_from_thread(
                                self.update_status,
                                f"Loaded from cache ({cache_time})",
                                "green",
                            )
                            self.app.call_from_thread(self.display_user_data)
                            return
                    except Exception:
                        pass
            
            # Fetch from API
            self.app.call_from_thread(
                self.update_status,
                f"Fetching user access for {user_id}...",
                "cyan",
            )
            
            auth = load_auth()
            user = powerbi_admin.User(auth=auth, user_id=user_id, verify=False)
            result = user()
            
            self.user_data = result
            
            # Save to cache
            if self.cache_manager:
                cache_key = f"user_access_{slugify(user_id)}"
                self.cache_manager.save(
                    cache_key, result, metadata={"user_id": user_id}
                )
            
            self.app.call_from_thread(
                self.update_status,
                f"Retrieved user access for {user_id}",
                "green",
            )
            self.app.call_from_thread(self.display_user_data)
            
        except Exception as e:
            self.app.call_from_thread(
                self.update_status, f"Error: {str(e)}", "red"
            )

    def display_user_data(self) -> None:
        """Display user data"""
        if not self.user_data:
            return
        
        result_container = self.query_one("#users-result", ScrollableContainer)
        result_static = result_container.query_one(Static)
        
        # Format as JSON for display
        result_text = json.dumps(self.user_data, indent=2)
        result_static.update(f"```json\n{result_text}\n```")

    @on(Button.Pressed, "#btn-get-user")
    def on_get_user(self) -> None:
        """Get user access information"""
        try:
            user_id_input = self.query_one("#user-id-input", Input)
            user_id = user_id_input.value.strip()
            
            if not user_id:
                self.update_status("Please enter a user ID (email)", "yellow")
                return
            
            self.update_status(f"Loading user access for {user_id}...", "cyan")
            self.get_user_access_worker(user_id)
            
        except Exception as e:
            self.update_status(f"Error: {str(e)}", "red")

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class PowerBITUI(App):
    """Power BI CLI Terminal User Interface"""

    CSS = """
    /* Power BI Color Palette */
    Screen {
        background: #1E1E1E;
    }

    Header {
        background: #00188F;
        color: #F2C811;
    }

    Footer {
        background: #252526;
        color: #E8E8E8;
    }

    .screen-title {
        background: #00188F;
        color: #F2C811;
        padding: 1;
        text-align: center;
        text-style: bold;
    }

    /* Main container */
    #main-container {
        height: auto;
    }

    /* Filter bar */
    #filter-bar {
        width: 100%;
        height: auto;
        padding: 1;
        background: #2D2D30;
    }

    .filter-selector {
        border: round #3E3E42;
        padding: 0 1;
        width: 1fr;
        margin: 0 1 0 0;
        background: #2D2D30;
        color: #E8E8E8;
    }

    .filter-input {
        border: round #3E3E42;
        padding: 0 1;
        width: 2fr;
        margin: 0 1 0 0;
        background: #2D2D30;
        color: #E8E8E8;
    }

    .filter-selector:focus, .filter-input:focus {
        border: round #00A4EF;
    }

    #btn-set-folder {
        min-width: 14;
    }

    /* Actions bar */
    #actions-bar {
        width: 100%;
        height: auto;
        padding: 1;
    }

    .action-buttons-bar Button {
        width: 1fr;
        margin: 0 1 0 0;
    }

    .action-buttons-bar Button:last-child {
        margin: 0;
    }

    Button {
        background: #2D2D30;
        color: #E8E8E8;
        border: round #3E3E42;
    }

    Button:hover {
        background: #3E3E42;
        border: round #00A4EF;
    }

    Button.-primary {
        background: #00188F;
        color: #F2C811;
    }

    Button.-primary:hover {
        background: #0020B0;
        border: round #F2C811;
    }

    Button.-warning {
        background: #FF8C00;
        color: #1E1E1E;
    }

    Button.-warning:hover {
        background: #FFA500;
    }

    Button.-default {
        background: #2D2D30;
    }

    /* Results container */
    #results-container {
        height: 1fr;
        border: round #3E3E42;
        margin: 1;
    }

    .panel-header {
        background: #2D2D30;
        color: #F2C811;
        padding: 1;
        text-style: bold;
    }

    #results-scroll {
        height: 1fr;
        background: #1E1E1E;
        padding: 1;
    }

    #results-content {
        color: #E8E8E8;
    }

    DataTable {
        background: #1E1E1E;
        color: #E8E8E8;
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: #2D2D30;
        color: #F2C811;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #00188F;
        color: #F2C811;
    }

    /* Screen containers */
    #workspaces-container, #apps-container, #reports-container, #users-container {
        padding: 1;
    }

    #workspaces-controls, #apps-controls, #reports-controls, #users-controls {
        height: auto;
        padding: 1 0;
    }

    #workspaces-controls Button, #apps-controls Button, #reports-controls Button, #users-controls Button {
        margin: 0 1 0 0;
    }

    #workspaces-status, #apps-status, #reports-status, #users-status {
        padding: 1 0;
        height: auto;
    }

    #workspaces-table, #apps-table {
        height: 1fr;
    }

    Input {
        border: round #3E3E42;
        background: #2D2D30;
        color: #E8E8E8;
        margin: 0 0 1 0;
    }

    Input:focus {
        border: round #00A4EF;
    }

    Label {
        color: #F2C811;
        padding: 1 0 0 0;
    }

    Select {
        border: round #3E3E42;
        background: #2D2D30;
        color: #E8E8E8;
        margin: 0 1 1 0;
    }

    Select:focus {
        border: round #00A4EF;
    }

    #role-selector {
        width: 20;
    }

    Static {
        color: #E8E8E8;
    }

    ScrollableContainer {
        height: 1fr;
        border: round #3E3E42;
        background: #1E1E1E;
        padding: 1;
    }
    """

    TITLE = "PowerBI CLI TUI"
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
    ]

    def on_mount(self) -> None:
        """Push the main menu screen on mount"""
        self.push_screen(MainMenuScreen())

    def action_quit(self) -> None:
        """Quit the application"""
        self.exit()


def run_tui():
    """Entry point to run the TUI application"""
    app = PowerBITUI()
    app.run()


if __name__ == "__main__":
    run_tui()
