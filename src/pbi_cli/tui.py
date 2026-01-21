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
        Binding("f", "filter", "Filter", show=True),
        Binding("o", "open", "Open", show=True),
        Binding("s", "search", "Search", show=True),
        Binding("n", "new", "New", show=True),
        Binding("c", "config", "Config", show=True),
        Binding("h", "help", "Help", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Create child widgets for the main menu."""
        yield Header()
        
        # Top filter bar - similar to JiraTUI
        yield Container(
            Horizontal(
                Container(
                    Label("Profile", classes="filter-label"),
                    Static("", id="filter_profile", classes="filter-value"),
                    classes="filter-item",
                ),
                Container(
                    Label("Output Folder", classes="filter-label"),
                    Static("", id="filter_output", classes="filter-value"),
                    classes="filter-item",
                ),
                Container(
                    Label("Action", classes="filter-label"),
                    Static("Select an action", id="filter_action", classes="filter-value"),
                    classes="filter-item",
                ),
                id="filter_bar",
            ),
            id="top_filter_section",
        )
        
        # Main content area - split left/right like JiraTUI
        yield Horizontal(
            # Left side - Table of options/items
            Container(
                Static("Actions", classes="panel-header"),
                DataTable(id="actions_table", zebra_stripes=True, cursor_type="row"),
                id="left_panel",
                classes="main-panel",
            ),
            # Right side - Details panel
            Container(
                Static("Details", classes="panel-header"),
                Container(
                    Static("Profile Information", classes="detail-section-title"),
                    Static("", id="detail_profile", classes="detail-text"),
                    Static("", id="detail_status", classes="detail-text"),
                    classes="detail-section",
                ),
                Container(
                    Static("Quick Actions", classes="detail-section-title"),
                    Static("• Press 'o' to open selected action", classes="detail-text"),
                    Static("• Press 'f' to change filters", classes="detail-text"),
                    Static("• Press 's' to search", classes="detail-text"),
                    Static("• Press 'c' for configuration", classes="detail-text"),
                    Static("• Press 'q' to quit", classes="detail-text"),
                    classes="detail-section",
                ),
                Container(
                    Static("Keyboard Shortcuts", classes="detail-section-title"),
                    Static("f1 - Help | f2 - Config | f3 - Profiles", classes="detail-text"),
                    classes="detail-section",
                ),
                id="right_panel",
                classes="main-panel",
            ),
            id="main_content_area",
        )
        
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the screen with data"""
        # Load profile info
        try:
            pbi_config = PBIConfig()
            active_profile = pbi_config.active_profile or "None"
            output_folder = pbi_config.default_output_folder or "Not set"
            
            # Update filters
            self.query_one("#filter_profile", Static).update(active_profile)
            self.query_one("#filter_output", Static).update(output_folder)
            
            # Update details panel
            self.query_one("#detail_profile", Static).update(f"Active Profile: {active_profile}")
            self.query_one("#detail_status", Static).update(f"Output Folder: {output_folder}")
            
            # Populate actions table
            table = self.query_one("#actions_table", DataTable)
            table.add_columns("#", "Action", "Shortcut", "Description")
            
            actions = [
                ("1", "Authentication", "1", "Manage authentication profiles"),
                ("2", "Configuration", "2", "Configure CLI settings"),
                ("3", "Workspaces", "3", "List and manage workspaces"),
                ("4", "Apps", "4", "View Power BI apps"),
                ("5", "Reports", "5", "Access report functions"),
                ("6", "Users", "6", "Manage user access"),
            ]
            
            for action in actions:
                table.add_row(*action)
                
        except Exception as e:
            self.query_one("#detail_status", Static).update(f"Error: {str(e)}")
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

    def action_filter(self) -> None:
        """Toggle filter options"""
        pass  # Placeholder for filter action

    def action_open(self) -> None:
        """Open selected action"""
        table = self.query_one("#actions_table", DataTable)
        if table.cursor_row is not None:
            try:
                row_key = table.cursor_row
                row = table.get_row(row_key)
                action_num = row[0]
                
                # Navigate based on action number
                if action_num == "1":
                    self.action_auth()
                elif action_num == "2":
                    self.action_config()
                elif action_num == "3":
                    self.action_workspaces()
                elif action_num == "4":
                    self.action_apps()
                elif action_num == "5":
                    self.action_reports()
                elif action_num == "6":
                    self.action_users()
            except Exception:
                pass  # No valid row selected

    def action_search(self) -> None:
        """Search action"""
        pass  # Placeholder for search

    def action_new(self) -> None:
        """New action"""
        pass  # Placeholder for new

    def action_help(self) -> None:
        """Show help"""
        pass  # Placeholder for help

    @on(DataTable.RowSelected)
    def on_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the actions table"""
        row = event.data_table.get_row(event.row_key)
        action_name = row[1]
        description = row[3]
        
        # Update details panel with selection info
        self.query_one("#filter_action", Static).update(action_name)

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
    /* Base colors - JiraTUI inspired dark theme with blue accents */
    * {
        scrollbar-background: #1e2124;
        scrollbar-color: #4a90d9;
    }
    
    Screen {
        background: #2b2b2b;
    }

    Header {
        background: #1e2124;
        color: #a0a0a0;
        height: 1;
    }

    Footer {
        background: #1e2124;
        color: #a0a0a0;
    }

    /* Top filter bar - similar to JiraTUI */
    #top_filter_section {
        height: auto;
        background: #1e2124;
        border: round #3c3f41;
        margin: 0 0 1 0;
        padding: 1;
    }

    #filter_bar {
        width: 100%;
        height: auto;
    }

    .filter-item {
        width: 1fr;
        height: auto;
        border: round #3c3f41;
        background: #313335;
        margin: 0 1 0 0;
        padding: 1;
    }

    .filter-item:last-child {
        margin: 0;
    }

    .filter-label {
        color: #6897bb;
        text-style: bold;
        padding: 0 0 0 0;
    }

    .filter-value {
        color: #a9b7c6;
        padding: 0 0 0 1;
    }

    /* Main content area - split left/right like JiraTUI */
    #main_content_area {
        width: 100%;
        height: 1fr;
    }

    .main-panel {
        height: 100%;
        border: round #3c3f41;
        background: #313335;
        padding: 0;
    }

    #left_panel {
        width: 60%;
        margin: 0 1 0 0;
    }

    #right_panel {
        width: 40%;
    }

    .panel-header {
        background: #1e2124;
        color: #6897bb;
        text-style: bold;
        padding: 1;
        border-bottom: solid #3c3f41;
    }

    /* Table styling - like JiraTUI */
    DataTable {
        background: #2b2b2b;
        height: 100%;
        margin: 0;
        padding: 0;
    }

    DataTable > .datatable--header {
        background: #1e2124;
        color: #6897bb;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #4a90d9;
        color: #ffffff;
        text-style: bold;
    }

    DataTable > .datatable--odd-row {
        background: #313335;
    }

    DataTable > .datatable--even-row {
        background: #2b2b2b;
    }

    DataTable > .datatable--hover {
        background: #3c3f41;
    }

    /* Details panel sections */
    .detail-section {
        border: round #3c3f41;
        background: #2b2b2b;
        padding: 1;
        margin: 1;
    }

    .detail-section-title {
        color: #6897bb;
        text-style: bold;
        padding: 0 0 1 0;
    }

    .detail-text {
        color: #a9b7c6;
        padding: 0 0 0 1;
    }

    /* Form elements */
    Button {
        height: 3;
        min-width: 16;
        background: #4a90d9;
        color: #ffffff;
        border: round #3c3f41;
    }

    Button:hover {
        background: #5ba3ef;
        color: #ffffff;
    }

    Button:focus {
        background: #6bb3ff;
        color: #ffffff;
        text-style: bold;
    }

    Button.-primary {
        background: #4a90d9;
        color: #ffffff;
    }

    Button.-success {
        background: #499c54;
        color: #ffffff;
    }

    Button.-error {
        background: #c75450;
        color: #ffffff;
    }

    Input {
        background: #2b2b2b;
        border: round #3c3f41;
        color: #a9b7c6;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    Input:focus {
        border: round #4a90d9;
    }

    Label {
        color: #a9b7c6;
        padding: 1 0 0 0;
    }

    Static#auth_status, Static#config_status, Static#workspaces_status,
    Static#apps_status, Static#reports_status, Static#users_status,
    Static#add_profile_status, Static#switch_profile_status,
    Static#delete_profile_status, Static#list_workspaces_status {
        padding: 1 0;
        color: #a9b7c6;
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
        border: round #3c3f41;
        background: #2b2b2b;
        margin: 1 0;
        padding: 1;
    }

    ListView {
        height: auto;
        max-height: 20;
        margin: 1 0;
        background: #2b2b2b;
        border: round #3c3f41;
    }

    ListItem {
        background: #313335;
        color: #a9b7c6;
        padding: 1;
    }

    ListItem:hover {
        background: #3c3f41;
        color: #ffffff;
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
