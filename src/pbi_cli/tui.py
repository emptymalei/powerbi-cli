"""
Terminal User Interface for PowerBI CLI using Textual

This module provides an interactive TUI for all PowerBI CLI functionalities,
making it easy to manage authentication, workspaces, apps, reports, and users.
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
    ListView,
    ListItem,
    Select,
    RadioButton,
    RadioSet,
    Checkbox,
    DataTable,
)
from textual.screen import Screen
from textual.binding import Binding
from textual import on
from rich.text import Text
from rich.table import Table as RichTable
from pathlib import Path
import json
from typing import Optional, Dict, Any

# Import the CLI modules we need
from pbi_cli.config import PBIConfig
from pbi_cli.cli import (
    load_auth,
    _load_profiles,
    _save_profiles,
    _set_credential,
    _get_credential,
    _delete_credential,
)
from pbi_cli.powerbi.admin import Workspaces, User, Apps
import pbi_cli.powerbi.app as powerbi_app


class MainMenuScreen(Screen):
    """Main menu screen for the PowerBI CLI TUI"""

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("1", "auth", "Auth", show=True),
        Binding("2", "config", "Config", show=True),
        Binding("3", "workspaces", "Workspaces", show=True),
        Binding("4", "apps", "Apps", show=True),
        Binding("5", "reports", "Reports", show=True),
        Binding("6", "users", "Users", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Create child widgets for the main menu."""
        yield Header()
        yield Vertical(
            # Top section - Navigation and Status
            Container(
                Horizontal(
                    # Navigation section
                    Container(
                        Static("Navigation", classes="section-title"),
                        Horizontal(
                            Container(Static("[1] Authentication", classes="nav-item"), id="nav_auth", classes="nav-box"),
                            Container(Static("[2] Configuration", classes="nav-item"), id="nav_config", classes="nav-box"),
                            Container(Static("[3] Workspaces", classes="nav-item"), id="nav_workspaces", classes="nav-box"),
                            classes="nav-row",
                        ),
                        Horizontal(
                            Container(Static("[4] Apps", classes="nav-item"), id="nav_apps", classes="nav-box"),
                            Container(Static("[5] Reports", classes="nav-item"), id="nav_reports", classes="nav-box"),
                            Container(Static("[6] Users", classes="nav-item"), id="nav_users", classes="nav-box"),
                            classes="nav-row",
                        ),
                        id="navigation_section",
                        classes="top-section-panel",
                    ),
                    # Status section
                    Container(
                        Static("Status", classes="section-title"),
                        Static("", id="profile_info", classes="status-item"),
                        Static("", id="output_info", classes="status-item"),
                        id="status_section",
                        classes="top-section-panel",
                    ),
                    id="top_content",
                ),
                id="top_section",
            ),
            # Bottom section - Main content area
            Container(
                Static("PowerBI CLI - Terminal Interface", classes="main-title"),
                Container(
                    Static("Quick Start Guide", classes="panel-subtitle"),
                    Static("• Press 1-6 to navigate to different sections", classes="info-text"),
                    Static("• Press Q to quit, ESC to go back", classes="info-text"),
                    Static("• All operations are saved to your configuration", classes="info-text"),
                    classes="info-panel",
                ),
                Container(
                    Static("Available Actions", classes="panel-subtitle"),
                    Static("", id="status_info", classes="status-detail"),
                    classes="info-panel",
                ),
                id="main_section",
            ),
            id="main_layout",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Display active profile info on mount"""
        try:
            pbi_config = PBIConfig()
            active_profile = pbi_config.active_profile
            output_folder = pbi_config.default_output_folder
            
            if active_profile:
                self.query_one("#profile_info", Static).update(
                    f"Profile: {active_profile}"
                )
                self.query_one("#output_info", Static).update(
                    f"Output Folder: {output_folder if output_folder else 'Not set'}"
                )
                self.query_one("#status_info", Static).update(
                    f"Ready to use PowerBI CLI\n"
                    f"Active profile: {active_profile}"
                )
            else:
                self.query_one("#profile_info", Static).update(
                    "No active profile"
                )
                self.query_one("#output_info", Static).update(
                    "Output Folder: Not set"
                )
                self.query_one("#status_info", Static).update(
                    "Please configure a profile in Authentication"
                )
        except Exception as e:
            self.query_one("#profile_info", Static).update(
                "Error loading config"
            )
            self.query_one("#status_info", Static).update(
                f"Error: {str(e)}"
            )

    def action_quit(self) -> None:
        """Quit the application."""
        self.app.exit()

    def action_auth(self) -> None:
        """Navigate to authentication screen."""
        self.app.push_screen(AuthScreen())

    def action_config(self) -> None:
        """Navigate to configuration screen."""
        self.app.push_screen(ConfigScreen())

    def action_workspaces(self) -> None:
        """Navigate to workspaces screen."""
        self.app.push_screen(WorkspacesScreen())

    def action_apps(self) -> None:
        """Navigate to apps screen."""
        self.app.push_screen(AppsScreen())

    def action_reports(self) -> None:
        """Navigate to reports screen."""
        self.app.push_screen(ReportsScreen())

    def action_users(self) -> None:
        """Navigate to users screen."""
        self.app.push_screen(UsersScreen())

    @on(Button.Pressed, "#btn_auth")
    def on_auth_button(self) -> None:
        self.action_auth()

    @on(Button.Pressed, "#btn_config")
    def on_config_button(self) -> None:
        self.action_config()

    @on(Button.Pressed, "#btn_workspaces")
    def on_workspaces_button(self) -> None:
        self.action_workspaces()

    @on(Button.Pressed, "#btn_apps")
    def on_apps_button(self) -> None:
        self.action_apps()

    @on(Button.Pressed, "#btn_reports")
    def on_reports_button(self) -> None:
        self.action_reports()

    @on(Button.Pressed, "#btn_users")
    def on_users_button(self) -> None:
        self.action_users()

    @on(Button.Pressed, "#btn_quit")
    def on_quit_button(self) -> None:
        self.action_quit()

    def on_click(self, event) -> None:
        """Handle click events on navigation boxes"""
        if hasattr(event, 'widget') and hasattr(event.widget, 'id'):
            widget_id = event.widget.id
            if widget_id == "nav_auth":
                self.action_auth()
            elif widget_id == "nav_config":
                self.action_config()
            elif widget_id == "nav_workspaces":
                self.action_workspaces()
            elif widget_id == "nav_apps":
                self.action_apps()
            elif widget_id == "nav_reports":
                self.action_reports()
            elif widget_id == "nav_users":
                self.action_users()


class AuthScreen(Screen):
    """Authentication management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
        Binding("a", "add_profile", "Add Profile", show=True),
        Binding("s", "switch_profile", "Switch", show=True),
        Binding("d", "delete_profile", "Delete", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Create authentication screen widgets"""
        yield Header()
        yield Container(
            Static("Authentication Management", id="auth_title"),
            Static("", id="auth_status"),
            DataTable(id="profiles_table", zebra_stripes=True),
            Horizontal(
                Button("Add Profile (A)", id="btn_add_profile", variant="success"),
                Button("Switch Profile (S)", id="btn_switch_profile", variant="primary"),
                Button("Delete Profile (D)", id="btn_delete_profile", variant="error"),
                Button("Refresh (R)", id="btn_refresh", variant="default"),
                id="auth_buttons",
            ),
            ScrollableContainer(id="auth_result"),
            id="auth_container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Load and display profiles when screen mounts"""
        self.load_profiles()

    def load_profiles(self) -> None:
        """Load and display authentication profiles"""
        try:
            profiles_data = _load_profiles()
            profiles = profiles_data.get("profiles", {})
            active_profile = profiles_data.get("active_profile")

            table = self.query_one("#profiles_table", DataTable)
            table.clear(columns=True)
            table.add_columns("Status", "Profile", "Active", "Token Exists")

            if not profiles:
                self.query_one("#auth_status", Static).update(
                    "[yellow]No profiles found. Use 'Add Profile' to create one.[/yellow]"
                )
                return

            for profile_name in profiles.keys():
                is_active = profile_name == active_profile
                token_exists = _get_credential(profile_name) is not None
                
                status = "✓" if token_exists else "✗"
                active_marker = "✓" if is_active else ""
                token_status = "Yes" if token_exists else "No"

                table.add_row(
                    status,
                    profile_name,
                    active_marker,
                    token_status,
                )

            self.query_one("#auth_status", Static).update(
                f"Active Profile: [bold green]{active_profile or 'None'}[/bold green]"
            )

        except Exception as e:
            self.query_one("#auth_status", Static).update(
                f"[red]Error loading profiles: {str(e)}[/red]"
            )

    def action_back(self) -> None:
        """Return to main menu"""
        self.app.pop_screen()

    def action_add_profile(self) -> None:
        """Add a new profile"""
        self.app.push_screen(AddProfileScreen())

    def action_switch_profile(self) -> None:
        """Switch active profile"""
        self.app.push_screen(SwitchProfileScreen())

    def action_delete_profile(self) -> None:
        """Delete a profile"""
        self.app.push_screen(DeleteProfileScreen())

    def action_refresh(self) -> None:
        """Refresh the profile list"""
        self.load_profiles()

    @on(Button.Pressed, "#btn_add_profile")
    def on_add_profile(self) -> None:
        self.action_add_profile()

    @on(Button.Pressed, "#btn_switch_profile")
    def on_switch_profile(self) -> None:
        self.action_switch_profile()

    @on(Button.Pressed, "#btn_delete_profile")
    def on_delete_profile(self) -> None:
        self.action_delete_profile()

    @on(Button.Pressed, "#btn_refresh")
    def on_refresh(self) -> None:
        self.action_refresh()


class AddProfileScreen(Screen):
    """Screen for adding a new profile"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Add New Profile", id="add_profile_title"),
            Static("Enter profile details:", id="add_profile_subtitle"),
            Label("Profile Name:"),
            Input(placeholder="e.g., default, production", id="profile_name_input"),
            Label("Bearer Token:"),
            Input(
                placeholder="Enter your bearer token (without 'Bearer' prefix)",
                password=True,
                id="token_input",
            ),
            Static("", id="add_profile_status"),
            Horizontal(
                Button("Save", id="btn_save", variant="success"),
                Button("Cancel", id="btn_cancel", variant="default"),
                id="add_profile_buttons",
            ),
            id="add_profile_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_save")
    def on_save(self) -> None:
        """Save the new profile"""
        try:
            profile_name = self.query_one("#profile_name_input", Input).value.strip()
            token = self.query_one("#token_input", Input).value.strip()

            if not profile_name:
                self.query_one("#add_profile_status", Static).update(
                    "[red]Profile name is required[/red]"
                )
                return

            if not token:
                self.query_one("#add_profile_status", Static).update(
                    "[red]Bearer token is required[/red]"
                )
                return

            # Remove "Bearer " prefix if present
            if token.startswith("Bearer "):
                token = token.replace("Bearer ", "")

            # Store token securely
            _set_credential(profile_name, token)

            # Update profiles configuration
            profiles_data = _load_profiles()
            if "profiles" not in profiles_data:
                profiles_data["profiles"] = {}

            profiles_data["profiles"][profile_name] = {"name": profile_name}

            # Set as active profile if it's the first one or if it's 'default'
            if not profiles_data.get("active_profile") or profile_name == "default":
                profiles_data["active_profile"] = profile_name

            _save_profiles(profiles_data)

            self.query_one("#add_profile_status", Static).update(
                f"[green]✓ Profile '{profile_name}' saved successfully![/green]"
            )

            # Return to auth screen after a moment
            self.set_timer(1.5, self.action_back)

        except Exception as e:
            self.query_one("#add_profile_status", Static).update(
                f"[red]Error saving profile: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self) -> None:
        self.action_back()


class SwitchProfileScreen(Screen):
    """Screen for switching active profile"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Switch Active Profile", id="switch_profile_title"),
            Static("Select a profile to activate:", id="switch_profile_subtitle"),
            ListView(id="profiles_list"),
            Static("", id="switch_profile_status"),
            Button("Back", id="btn_back", variant="default"),
            id="switch_profile_container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Load profiles when screen mounts"""
        self.load_profiles()

    def load_profiles(self) -> None:
        """Load available profiles"""
        try:
            profiles_data = _load_profiles()
            profiles = profiles_data.get("profiles", {})
            active_profile = profiles_data.get("active_profile")

            list_view = self.query_one("#profiles_list", ListView)
            list_view.clear()

            if not profiles:
                self.query_one("#switch_profile_subtitle", Static).update(
                    "[yellow]No profiles found[/yellow]"
                )
                return

            for profile_name in profiles.keys():
                is_active = profile_name == active_profile
                marker = " (active)" if is_active else ""
                list_view.append(ListItem(Label(f"{profile_name}{marker}")))

        except Exception as e:
            self.query_one("#switch_profile_status", Static).update(
                f"[red]Error loading profiles: {str(e)}[/red]"
            )

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(ListView.Selected)
    def on_profile_selected(self, event: ListView.Selected) -> None:
        """Handle profile selection"""
        try:
            # Get the selected profile name
            label = event.item.query_one(Label)
            profile_name = label.renderable.split(" (active)")[0].strip()

            # Update active profile
            profiles_data = _load_profiles()
            profiles_data["active_profile"] = profile_name
            _save_profiles(profiles_data)

            self.query_one("#switch_profile_status", Static).update(
                f"[green]✓ Switched to profile '{profile_name}'[/green]"
            )

            # Return to auth screen
            self.set_timer(1.5, self.action_back)

        except Exception as e:
            self.query_one("#switch_profile_status", Static).update(
                f"[red]Error switching profile: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_back")
    def on_back(self) -> None:
        self.action_back()


class DeleteProfileScreen(Screen):
    """Screen for deleting a profile"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Delete Profile", id="delete_profile_title"),
            Static("Select a profile to delete:", id="delete_profile_subtitle"),
            ListView(id="delete_profiles_list"),
            Static("", id="delete_profile_status"),
            Horizontal(
                Button("Delete Selected", id="btn_delete", variant="error"),
                Button("Cancel", id="btn_cancel", variant="default"),
                id="delete_profile_buttons",
            ),
            id="delete_profile_container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Load profiles when screen mounts"""
        self.selected_profile = None
        self.load_profiles()

    def load_profiles(self) -> None:
        """Load available profiles"""
        try:
            profiles_data = _load_profiles()
            profiles = profiles_data.get("profiles", {})

            list_view = self.query_one("#delete_profiles_list", ListView)
            list_view.clear()

            if not profiles:
                self.query_one("#delete_profile_subtitle", Static).update(
                    "[yellow]No profiles found[/yellow]"
                )
                return

            for profile_name in profiles.keys():
                list_view.append(ListItem(Label(profile_name)))

        except Exception as e:
            self.query_one("#delete_profile_status", Static).update(
                f"[red]Error loading profiles: {str(e)}[/red]"
            )

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(ListView.Selected)
    def on_profile_selected(self, event: ListView.Selected) -> None:
        """Store the selected profile"""
        label = event.item.query_one(Label)
        self.selected_profile = label.renderable.strip()
        self.query_one("#delete_profile_status", Static).update(
            f"Selected: [yellow]{self.selected_profile}[/yellow]"
        )

    @on(Button.Pressed, "#btn_delete")
    def on_delete(self) -> None:
        """Delete the selected profile"""
        if not self.selected_profile:
            self.query_one("#delete_profile_status", Static).update(
                "[red]Please select a profile to delete[/red]"
            )
            return

        try:
            profiles_data = _load_profiles()

            if self.selected_profile not in profiles_data.get("profiles", {}):
                self.query_one("#delete_profile_status", Static).update(
                    f"[red]Profile '{self.selected_profile}' not found[/red]"
                )
                return

            # Delete credential
            _delete_credential(self.selected_profile)

            # Remove from profiles
            del profiles_data["profiles"][self.selected_profile]

            # If this was the active profile, clear it or switch to another
            if profiles_data.get("active_profile") == self.selected_profile:
                remaining_profiles = list(profiles_data["profiles"].keys())
                profiles_data["active_profile"] = (
                    remaining_profiles[0] if remaining_profiles else None
                )

            _save_profiles(profiles_data)

            self.query_one("#delete_profile_status", Static).update(
                f"[green]✓ Profile '{self.selected_profile}' deleted successfully[/green]"
            )

            # Return to auth screen
            self.set_timer(1.5, self.action_back)

        except Exception as e:
            self.query_one("#delete_profile_status", Static).update(
                f"[red]Error deleting profile: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self) -> None:
        self.action_back()


class ConfigScreen(Screen):
    """Configuration management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Configuration Settings", id="config_title"),
            Static("", id="config_current"),
            Label("Set Default Output Folder:"),
            Input(placeholder="e.g., ~/PowerBI/backups", id="output_folder_input"),
            Horizontal(
                Button("Save", id="btn_save_config", variant="success"),
                Button("Clear", id="btn_clear_config", variant="warning"),
                id="config_buttons",
            ),
            Static("", id="config_status"),
            id="config_container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Load current configuration"""
        self.load_config()

    def load_config(self) -> None:
        """Load and display current configuration"""
        try:
            pbi_config = PBIConfig()
            current_folder = pbi_config.default_output_folder or "Not set"
            
            self.query_one("#config_current", Static).update(
                f"Current output folder: [cyan]{current_folder}[/cyan]"
            )

            if pbi_config.default_output_folder:
                self.query_one("#output_folder_input", Input).value = (
                    pbi_config.default_output_folder
                )

        except Exception as e:
            self.query_one("#config_status", Static).update(
                f"[red]Error loading config: {str(e)}[/red]"
            )

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_save_config")
    def on_save_config(self) -> None:
        """Save the configuration"""
        try:
            folder_path = self.query_one("#output_folder_input", Input).value.strip()

            if not folder_path:
                self.query_one("#config_status", Static).update(
                    "[yellow]Please enter a folder path[/yellow]"
                )
                return

            # Strip quotes
            folder_path_clean = folder_path.strip('"').strip("'")

            pbi_config = PBIConfig()
            pbi_config.default_output_folder = folder_path_clean
            
            resolved_path = Path(folder_path_clean).expanduser().absolute()

            self.query_one("#config_status", Static).update(
                f"[green]✓ Default output folder set to: {resolved_path}[/green]"
            )

            # Reload config
            self.load_config()

        except Exception as e:
            self.query_one("#config_status", Static).update(
                f"[red]Error saving config: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_clear_config")
    def on_clear_config(self) -> None:
        """Clear the output folder configuration"""
        try:
            pbi_config = PBIConfig()
            pbi_config.default_output_folder = None

            self.query_one("#config_status", Static).update(
                "[green]✓ Output folder configuration cleared[/green]"
            )

            # Reload config
            self.load_config()

        except Exception as e:
            self.query_one("#config_status", Static).update(
                f"[red]Error clearing config: {str(e)}[/red]"
            )


class WorkspacesScreen(Screen):
    """Workspaces management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
        Binding("l", "list_workspaces", "List", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Workspaces Management", id="workspaces_title"),
            Static("Manage Power BI Workspaces", id="workspaces_subtitle"),
            Vertical(
                Button("List Workspaces (L)", id="btn_list_workspaces", variant="primary"),
                Button("Back (Esc)", id="btn_back", variant="default"),
                id="workspaces_buttons",
            ),
            ScrollableContainer(id="workspaces_result"),
            id="workspaces_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_list_workspaces(self) -> None:
        """Navigate to list workspaces screen"""
        self.app.push_screen(ListWorkspacesScreen())

    @on(Button.Pressed, "#btn_list_workspaces")
    def on_list_workspaces(self) -> None:
        self.action_list_workspaces()

    @on(Button.Pressed, "#btn_back")
    def on_back(self) -> None:
        self.action_back()


class ListWorkspacesScreen(Screen):
    """Screen for listing workspaces"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("List Workspaces", id="list_workspaces_title"),
            Label("Top N results:"),
            Input(value="100", id="top_input"),
            Static("This operation requires admin access", id="list_workspaces_note"),
            Horizontal(
                Button("List", id="btn_list", variant="primary"),
                Button("Cancel", id="btn_cancel", variant="default"),
                id="list_workspaces_buttons",
            ),
            Static("", id="list_workspaces_status"),
            DataTable(id="workspaces_table", zebra_stripes=True),
            id="list_workspaces_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_list")
    def on_list(self) -> None:
        """List workspaces"""
        try:
            top = int(self.query_one("#top_input", Input).value)

            self.query_one("#list_workspaces_status", Static).update(
                "[cyan]Loading workspaces...[/cyan]"
            )

            # Get auth
            auth = load_auth()

            # Create Workspaces instance
            workspaces = Workspaces(auth=auth, verify=False)

            # Fetch workspaces
            result = workspaces(top=top, expand=[], filter=None)

            # Display results
            table = self.query_one("#workspaces_table", DataTable)
            table.clear(columns=True)

            if result and "value" in result and len(result["value"]) > 0:
                # Add columns
                table.add_columns("ID", "Name", "Type", "State")

                # Add rows
                for workspace in result["value"]:
                    table.add_row(
                        workspace.get("id", ""),
                        workspace.get("name", ""),
                        workspace.get("type", ""),
                        workspace.get("state", ""),
                    )

                self.query_one("#list_workspaces_status", Static).update(
                    f"[green]✓ Found {len(result['value'])} workspace(s)[/green]"
                )
            else:
                self.query_one("#list_workspaces_status", Static).update(
                    "[yellow]No workspaces found[/yellow]"
                )

        except Exception as e:
            self.query_one("#list_workspaces_status", Static).update(
                f"[red]Error: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self) -> None:
        self.action_back()


class AppsScreen(Screen):
    """Apps management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Apps Management", id="apps_title"),
            Static("Manage Power BI Apps", id="apps_subtitle"),
            Vertical(
                Button("List Apps", id="btn_list_apps", variant="primary"),
                Button("Back (Esc)", id="btn_back", variant="default"),
                id="apps_buttons",
            ),
            Static("", id="apps_status"),
            DataTable(id="apps_table", zebra_stripes=True),
            id="apps_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_list_apps")
    def on_list_apps(self) -> None:
        """List Power BI apps"""
        try:
            self.query_one("#apps_status", Static).update(
                "[cyan]Loading apps...[/cyan]"
            )

            # Get auth
            auth = load_auth()

            # Create Apps instance (as user)
            apps = powerbi_app.Apps(auth=auth, verify=False)

            # Fetch apps
            result = apps()

            # Display results
            table = self.query_one("#apps_table", DataTable)
            table.clear(columns=True)

            if result and "value" in result and len(result["value"]) > 0:
                # Add columns
                table.add_columns("ID", "Name", "Description")

                # Add rows
                for app in result["value"]:
                    table.add_row(
                        app.get("id", "")[:20] + "...",  # Truncate ID
                        app.get("name", ""),
                        app.get("description", "")[:50] if app.get("description") else "",
                    )

                self.query_one("#apps_status", Static).update(
                    f"[green]✓ Found {len(result['value'])} app(s)[/green]"
                )
            else:
                self.query_one("#apps_status", Static).update(
                    "[yellow]No apps found[/yellow]"
                )

        except Exception as e:
            self.query_one("#apps_status", Static).update(
                f"[red]Error: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_back")
    def on_back(self) -> None:
        self.action_back()


class ReportsScreen(Screen):
    """Reports management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Reports Management", id="reports_title"),
            Static("Manage Power BI Reports", id="reports_subtitle"),
            Static(
                "Report functionality is available through Workspaces and Apps screens",
                id="reports_note",
            ),
            Button("Back (Esc)", id="btn_back", variant="default"),
            id="reports_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_back")
    def on_back(self) -> None:
        self.action_back()


class UsersScreen(Screen):
    """Users management screen"""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Users Management", id="users_title"),
            Static("Get user access information", id="users_subtitle"),
            Label("User ID (email):"),
            Input(placeholder="user@example.com", id="user_id_input"),
            Horizontal(
                Button("Get User Access", id="btn_get_user", variant="primary"),
                Button("Cancel", id="btn_cancel", variant="default"),
                id="users_buttons",
            ),
            Static("", id="users_status"),
            ScrollableContainer(id="users_result"),
            id="users_container",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn_get_user")
    def on_get_user(self) -> None:
        """Get user access information"""
        try:
            user_id = self.query_one("#user_id_input", Input).value.strip()

            if not user_id:
                self.query_one("#users_status", Static).update(
                    "[yellow]Please enter a user ID[/yellow]"
                )
                return

            self.query_one("#users_status", Static).update(
                "[cyan]Loading user access information...[/cyan]"
            )

            # Get auth
            auth = load_auth()

            # Create User instance
            user = User(auth=auth, user_id=user_id, verify=False)

            # Fetch user data
            result = user()

            # Display results
            result_container = self.query_one("#users_result", ScrollableContainer)
            result_container.remove_children()

            if result:
                # Format as JSON for display
                result_text = json.dumps(result, indent=2)
                result_container.mount(Static(f"```json\n{result_text}\n```"))

                self.query_one("#users_status", Static).update(
                    f"[green]✓ Retrieved user access info for {user_id}[/green]"
                )
            else:
                self.query_one("#users_status", Static).update(
                    "[yellow]No data found for user[/yellow]"
                )

        except Exception as e:
            self.query_one("#users_status", Static).update(
                f"[red]Error: {str(e)}[/red]"
            )

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self) -> None:
        self.action_back()


class PowerBITUI(App):
    """Power BI CLI Terminal User Interface"""

    CSS = """
    /* Base colors - calm, professional palette */
    * {
        scrollbar-background: #1a1f2e;
        scrollbar-color: #3d4859;
    }
    
    Screen {
        background: #1a1f2e;
    }

    Header {
        background: #252b3b;
        color: #8b95a8;
        height: 1;
    }

    Footer {
        background: #252b3b;
        color: #8b95a8;
    }

    /* Main layout - vertical split */
    #main_layout {
        width: 100%;
        height: 100%;
        background: #1a1f2e;
    }

    /* Top section - Navigation and Status */
    #top_section {
        height: auto;
        max-height: 12;
        background: #1a1f2e;
        border: round #3d4859;
        margin: 0 1 1 1;
        padding: 1;
    }

    #top_content {
        width: 100%;
        height: auto;
    }

    .top-section-panel {
        width: 1fr;
        height: auto;
        border: round #3d4859;
        background: #252b3b;
        padding: 1;
        margin: 0 1 0 0;
    }

    .top-section-panel:last-child {
        margin: 0;
    }

    #navigation_section {
        width: 2fr;
    }

    #status_section {
        width: 1fr;
    }

    .section-title {
        text-style: bold;
        color: #6d8299;
        margin: 0 0 1 0;
    }

    /* Navigation boxes */
    .nav-row {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }

    .nav-row:last-child {
        margin: 0;
    }

    .nav-box {
        width: 1fr;
        height: 3;
        border: round #3d4859;
        background: #2d3548;
        margin: 0 1 0 0;
        padding: 0 1;
        content-align: center middle;
    }

    .nav-box:last-child {
        margin: 0;
    }

    .nav-box:hover {
        background: #3d4859;
        border: round #4a6580;
    }

    .nav-box:focus-within {
        background: #4a5568;
        border: round #5a7590;
    }

    .nav-item {
        color: #8b95a8;
        text-align: center;
        width: 100%;
    }

    .nav-box:hover .nav-item {
        color: #a3b5cc;
        text-style: bold;
    }

    /* Status items */
    .status-item {
        color: #8b95a8;
        padding: 0 0 1 0;
        background: #1a1f2e;
        border: round #3d4859;
        margin: 0 0 1 0;
        padding: 1;
    }

    .status-item:last-child {
        margin: 0;
    }

    /* Main section - Bottom area */
    #main_section {
        height: 1fr;
        background: #1a1f2e;
        border: round #3d4859;
        margin: 0 1 1 1;
        padding: 2;
        overflow-y: auto;
    }

    .main-title {
        text-style: bold;
        color: #6d8299;
        padding: 0 0 2 0;
        text-align: center;
    }

    .info-panel {
        border: round #3d4859;
        background: #252b3b;
        padding: 2;
        margin: 0 0 2 0;
    }

    .info-panel:last-child {
        margin: 0;
    }

    .panel-subtitle {
        text-style: bold;
        color: #6d8299;
        padding: 0 0 1 0;
    }

    .info-text {
        color: #8b95a8;
        padding: 0 0 1 1;
    }

    .info-text:last-child {
        padding: 0 0 0 1;
    }

    .status-detail {
        color: #8b95a8;
        padding: 1;
        background: #1a1f2e;
        border: round #3d4859;
    }

    /* Form elements */
    Button {
        height: 3;
        min-width: 16;
        background: #3d4859;
        color: #8b95a8;
        border: round #3d4859;
    }

    Button:hover {
        background: #4a5568;
        color: #a3b5cc;
    }

    Button:focus {
        background: #556680;
        color: #c5d1e0;
        text-style: bold;
    }

    Button.-primary {
        background: #3d5a7a;
        color: #a3c5e0;
    }

    Button.-success {
        background: #3d5a4a;
        color: #a3d5b5;
    }

    Button.-error {
        background: #5a3d3d;
        color: #d5a3a3;
    }

    Input {
        background: #1a1f2e;
        border: round #3d4859;
        color: #a3b5cc;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    Input:focus {
        border: round #4a6580;
    }

    Label {
        color: #8b95a8;
        padding: 1 0 0 0;
    }

    DataTable {
        background: #1a1f2e;
        border: round #3d4859;
        height: auto;
        max-height: 25;
        margin: 1 0;
    }

    DataTable > .datatable--header {
        background: #2d3548;
        color: #8b95a8;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #3d4859;
        color: #c5d1e0;
    }

    Static#auth_status, Static#config_status, Static#workspaces_status,
    Static#apps_status, Static#reports_status, Static#users_status,
    Static#add_profile_status, Static#switch_profile_status,
    Static#delete_profile_status, Static#list_workspaces_status {
        padding: 1 0;
        color: #8b95a8;
    }

    #auth_buttons, #config_buttons, #workspaces_buttons,
    #apps_buttons, #reports_buttons, #users_buttons,
    #add_profile_buttons, #delete_profile_buttons,
    #list_workspaces_buttons {
        width: 100%;
        height: auto;
        padding: 1 0;
    }

    Horizontal > Button {
        width: 1fr;
        margin: 0 1 0 0;
    }

    Horizontal > Button:last-child {
        margin: 0;
    }

    ScrollableContainer {
        height: auto;
        max-height: 20;
        border: round #3d4859;
        background: #1a1f2e;
        margin: 1 0;
        padding: 1;
    }

    ListView {
        height: auto;
        max-height: 20;
        margin: 1 0;
        background: #1a1f2e;
        border: round #3d4859;
    }

    ListItem {
        background: #252b3b;
        color: #8b95a8;
        padding: 1;
    }

    ListItem:hover {
        background: #3d4859;
        color: #a3b5cc;
    }

    /* Container styling */
    Container {
        background: transparent;
    }

    Vertical {
        background: transparent;
    }

    Horizontal {
        background: transparent;
    }
    """

    def on_mount(self) -> None:
        """Setup the application on mount"""
        self.title = "PowerBI CLI - Interactive TUI"
        self.sub_title = "Press ? for help"
        self.push_screen(MainMenuScreen())


def run():
    """Run the PowerBI TUI application"""
    app = PowerBITUI()
    app.run()


if __name__ == "__main__":
    run()
