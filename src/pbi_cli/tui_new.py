"""
Simple Terminal User Interface for PowerBI CLI

Clean, minimal TUI with command selection and output display.
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, Select, Static

import pbi_cli.powerbi.admin as powerbi_admin
import pbi_cli.powerbi.app as powerbi_app
from pbi_cli.cli import _load_profiles, load_auth


class PowerBITUI(App):
    """Simple Power BI CLI Terminal User Interface"""

    CSS = """
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

    #title-bar {
        height: 3;
        background: #00188F;
        color: #F2C811;
        content-align: center middle;
        text-style: bold;
        border: heavy #F2C811;
    }

    #top-pane {
        height: 9;
        background: #2D2D30;
        border: round #F2C811;
        padding: 1;
        margin: 0 1 1 1;
    }

    #info-row {
        height: 3;
        width: 100%;
    }

    #controls-row {
        height: 5;
        width: 100%;
        padding: 1 0;
    }

    .info-label {
        color: #F2C811;
        text-style: bold;
        padding: 0 1;
    }

    .control-label {
        color: #F2C811;
        padding: 0 1;
        width: auto;
    }

    Select {
        width: 1fr;
        margin: 0 1;
        background: #2D2D30;
        color: #E8E8E8;
        border: round #3E3E42;
    }

    Select:focus {
        border: round #00A4EF;
    }

    Button {
        margin: 0 1;
        background: #00188F;
        color: #F2C811;
    }

    Button:hover {
        background: #0020B0;
    }

    #bottom-pane {
        height: 1fr;
        border: round #F2C811;
        margin: 1;
        padding: 1;
        background: #1E1E1E;
    }

    #output-content {
        color: #E8E8E8;
        width: 100%;
        height: 100%;
    }
    """

    TITLE = "PowerBI CLI"
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.active_profile = "None"
        self.command_group = "workspaces"
        self.command = "list"

    def compose(self) -> ComposeResult:
        """Create the UI layout"""
        yield Header()

        # Title
        yield Static("🔷 Microsoft Power BI CLI", id="title-bar")

        # Top pane - Controls
        with Container(id="top-pane"):
            # Info row
            with Horizontal(id="info-row"):
                yield Label(
                    f"Active Profile: {self.active_profile}",
                    classes="info-label",
                    id="profile-info",
                )

            # Controls row
            with Horizontal(id="controls-row"):
                yield Label("Command Group:", classes="control-label")
                yield Select(
                    options=[
                        ("Workspaces", "workspaces"),
                        ("Apps", "apps"),
                    ],
                    value="workspaces",
                    id="group-selector",
                )
                yield Label("Command:", classes="control-label")
                yield Select(
                    options=[("List", "list")],
                    value="list",
                    id="command-selector",
                )
                yield Button("Run", variant="primary", id="run-button")

        # Bottom pane - Output
        with Container(id="bottom-pane"):
            yield Static("Ready. Select a command and click Run.", id="output-content")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize when app starts"""
        self.load_active_profile()

    def load_active_profile(self) -> None:
        """Load the active profile name"""
        try:
            profiles_data = _load_profiles()
            self.active_profile = profiles_data.get("active_profile") or "None"
            profile_label = self.query_one("#profile-info", Label)
            profile_label.update(f"Active Profile: {self.active_profile}")
        except Exception as e:
            self.active_profile = "None"
            profile_label = self.query_one("#profile-info", Label)
            profile_label.update(f"Active Profile: Error - {str(e)}")

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle dropdown changes"""
        if event.select.id == "group-selector":
            self.command_group = event.value
            # Update command options based on group
            command_selector = self.query_one("#command-selector", Select)
            if self.command_group in ["workspaces", "apps"]:
                command_selector.set_options([("List", "list")])
                command_selector.value = "list"
        elif event.select.id == "command-selector":
            self.command = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks"""
        if event.button.id == "run-button":
            self.run_command()

    def run_command(self) -> None:
        """Execute the selected command"""
        output = self.query_one("#output-content", Static)

        try:
            output.update(
                f"[yellow]Running {self.command_group} {self.command}...[/yellow]"
            )

            # Load authentication
            auth = load_auth()

            if self.command_group == "workspaces" and self.command == "list":
                # Get workspaces
                workspaces_api = powerbi_admin.Workspaces(auth=auth, verify=False)
                result = workspaces_api.get(top=100)

                # Format output
                if result and "value" in result:
                    workspaces = result["value"]
                    output_text = (
                        f"[green]Found {len(workspaces)} workspaces:[/green]\n\n"
                    )
                    for ws in workspaces[:20]:  # Show first 20
                        name = ws.get("name", "N/A")
                        ws_id = ws.get("id", "N/A")
                        output_text += f"• {name}\n  ID: {ws_id}\n\n"
                    if len(workspaces) > 20:
                        output_text += (
                            f"[yellow]... and {len(workspaces) - 20} more[/yellow]"
                        )
                    output.update(output_text)
                else:
                    output.update("[red]No workspaces found[/red]")

            elif self.command_group == "apps" and self.command == "list":
                # Get apps
                apps_api = powerbi_app.Apps(auth=auth, verify=False)
                result = apps_api.get()

                # Format output
                if result and "value" in result:
                    apps = result["value"]
                    output_text = f"[green]Found {len(apps)} apps:[/green]\n\n"
                    for app in apps[:20]:  # Show first 20
                        name = app.get("name", "N/A")
                        app_id = app.get("id", "N/A")
                        output_text += f"• {name}\n  ID: {app_id}\n\n"
                    if len(apps) > 20:
                        output_text += f"[yellow]... and {len(apps) - 20} more[/yellow]"
                    output.update(output_text)
                else:
                    output.update("[red]No apps found[/red]")

            else:
                output.update(
                    f"[red]Unknown command: {self.command_group} {self.command}[/red]"
                )

        except Exception as e:
            output.update(f"[red]Error: {str(e)}[/red]")

    def action_refresh(self) -> None:
        """Refresh action"""
        self.load_active_profile()
        output = self.query_one("#output-content", Static)
        output.update("Refreshed. Select a command and click Run.")

    def action_quit(self) -> None:
        """Quit the application"""
        self.exit()


def run_tui():
    """Entry point to run the TUI application"""
    app = PowerBITUI()
    app.run()


if __name__ == "__main__":
    run_tui()
