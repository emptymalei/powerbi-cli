# Terminal User Interface (TUI)

The PowerBI CLI includes a modern Terminal User Interface (TUI) that provides an interactive way to use all CLI functionalities.

## Features

The TUI provides an easy-to-use interface for:

- **Authentication Management**: Add, switch, and delete authentication profiles
- **Configuration Settings**: Manage default output folders and other settings
- **Workspaces Management**: List and manage Power BI workspaces
- **Apps Management**: List and manage Power BI apps
- **Reports Management**: Access report-related functionality
- **Users Management**: Query user access information

## Launching the TUI

To launch the TUI, simply run:

```bash
pbi tui
```

## Navigation

The TUI supports both keyboard shortcuts and mouse clicks for navigation:

### Main Menu Shortcuts

- **1-6**: Navigate directly to different sections (Auth, Config, Workspaces, Apps, Reports, Users)
- **Q**: Quit the application
- **ESC**: Go back to the previous screen

### Screen-Specific Shortcuts

Each screen has its own set of shortcuts displayed in the footer:

- **Authentication Screen**:
  - `A`: Add a new profile
  - `S`: Switch active profile
  - `D`: Delete a profile
  - `R`: Refresh the profile list

- **General Navigation**:
  - `ESC`: Return to the previous screen
  - `Tab`: Navigate between input fields and buttons
  - `Enter`: Activate buttons or select items

## Authentication Management

The TUI allows you to manage multiple authentication profiles:

1. **Add a Profile**: Enter a profile name and your bearer token
2. **Switch Profiles**: Select which profile to use as active
3. **Delete Profiles**: Remove profiles you no longer need

All credentials are stored securely using the system keyring (or encrypted file storage as a fallback).

## Configuration Settings

Set and manage configuration options:

- **Default Output Folder**: Set a default location for exported files
- View current configuration settings
- Clear configuration values

## Workspaces

List and view Power BI workspaces:

- Fetch workspaces using the admin API
- View workspace details in a table format
- Specify the number of results to retrieve

## Apps

Manage Power BI apps:

- List all apps you have access to
- View app details including ID, name, and description
- Export app data

## Reports

Access report-related functionality through the workspaces and apps screens.

## Users

Query user access information:

- Enter a user ID (email address)
- Retrieve and display user access details
- View results in formatted JSON

## Benefits of Using the TUI

1. **Ease of Use**: No need to remember complex CLI commands
2. **Visual Feedback**: See your configuration and profiles at a glance
3. **Interactive**: Navigate through options with keyboard shortcuts
4. **Guided Workflow**: Clear prompts and instructions for each action
5. **Error Handling**: Immediate feedback on errors with helpful messages

## Fallback to CLI

All TUI functionality is also available through standard CLI commands. The TUI is built on top of the existing CLI infrastructure, so you can switch between TUI and CLI as needed.

## Requirements

The TUI requires the `textual` library, which is automatically installed with the PowerBI CLI:

```bash
pip install pbi-cli
```

## Technical Details

The TUI is built using [Textual](https://github.com/Textualize/textual), a modern Python framework for building terminal user interfaces. Textual provides:

- Rich, colorful interfaces
- Mouse support
- Responsive layouts
- Cross-platform compatibility

## Troubleshooting

### TUI Not Launching

If you see an error about missing dependencies:

```bash
pip install textual
```

### Display Issues

Ensure your terminal supports ANSI colors and has a reasonable size (at least 80x24 characters).

### Terminal Compatibility

The TUI works best with modern terminal emulators:
- **Linux**: GNOME Terminal, Konsole, Alacritty
- **macOS**: Terminal.app, iTerm2
- **Windows**: Windows Terminal, ConEmu

## Feedback and Issues

If you encounter any issues with the TUI, please report them on the [GitHub repository](https://github.com/emptymalei/powerbi-cli/issues).
