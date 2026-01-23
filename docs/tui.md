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

### Main Menu

![TUI Main Menu](images/tui_01_welcome.svg)

The main interface provides quick access to all major functionalities through numbered action buttons and functional controls at the top for profile switching and output folder configuration.

## Navigation

The TUI supports both keyboard shortcuts and mouse clicks for navigation:

### Main Menu Shortcuts

- **1**: Settings & Profiles (combined authentication and configuration)
- **2**: Workspaces (list workspaces with caching)
- **3**: Apps (list apps with caching)
- **4**: Reports (export reports with input fields)
- **5**: Users (query user access with input fields)
- **6**: Help (display help information)
- **Q** or **Ctrl+C**: Quit the application
- **Ctrl+R**: Refresh the current view

### Navigation Tips

- Use the **Profile dropdown** at the top to switch between authentication profiles
- Use the **Output Folder input** and "Set Folder" button to update configuration
- **Tab**: Navigate between input fields, buttons, and selects
- **Enter**: Activate buttons, select dropdown items, or submit forms
- **Arrow keys**: Navigate within dropdowns and tables

## Settings & Profiles Management

![TUI Settings & Profiles Screen](images/tui_02_settings_profiles.svg)

The TUI allows you to manage authentication profiles and configuration in one place:

### Authentication Profiles
1. **Add a Profile**: Enter a profile name and your bearer token
2. **Switch Profiles**: Use the profile selector dropdown at the top to switch active profiles
3. **Delete Profiles**: Remove profiles you no longer need

### Configuration Settings
- **Output Folder**: Set a default location for exported files using the input field and "Set Folder" button
- View current active profile and output folder in the top bar
- All configuration is automatically saved

All credentials are stored securely using the system keyring (or encrypted file storage as a fallback).

## Workspaces

![TUI Workspaces Screen](images/tui_03_workspaces.svg)

List and view Power BI workspaces with intelligent caching:

- **Load Workspaces**: Fetch workspaces using the admin API (uses cache if available)
- **Refresh Cache**: Force a fresh API call to update workspace data
- **Caching**: Results are cached for 1 hour to improve performance
- View workspace details in a table format with ID, name, type, and state

## Apps

![TUI Apps Screen](images/tui_04_apps.svg)

Manage Power BI apps with intelligent caching:

- **Load Apps**: List all apps you have access to (uses cache if available)
- **Refresh Cache**: Force a fresh API call to update app data
- **Caching**: Results are cached for 1 hour to improve performance
- View app details including ID, name, and description in table format

## Reports

![TUI Reports Screen](images/tui_05_reports.svg)

Export and download Power BI reports with full parameter support:

- **Source File**: Enter the path to the source report file
- **Target File**: Specify where to save the exported report
- **File Type**: Select from csv, tsv, json, xlsx, or parquet formats
- All parameters match the CLI command options

## Users

![TUI Users Screen](images/tui_06_users.svg)

Query user access information with full parameter support:

- **User ID**: Enter a user's email address
- **Target Folder**: Specify where to save user access data
- **File Types**: Choose which types of data to include (apps, groups, capacities)
- **File Name**: Customize the output file name
- View results in formatted JSON or export to file

## Help

![TUI Help Screen](images/tui_07_help.svg)

Access built-in help and keyboard shortcuts:

- View all available keyboard shortcuts
- Quick reference for navigation
- Feature descriptions and usage tips
- Press **6** or click the Help button to access help at any time

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

The TUI requires the `textual` library, which is automatically installed as a dependency when you install PowerBI CLI. If you encounter import errors, try reinstalling:

**With uv (Recommended):**
```bash
uv pip install --force-reinstall pbi-cli
# Or from source:
uv pip install --force-reinstall -e .
```

**With pip:**
```bash
pip install --upgrade --force-reinstall pbi-cli
```

**With Poetry:**
```bash
poetry install --sync
```

This will ensure all required dependencies, including Textual (>=7.3.0), are properly installed.

See [UV_INSTALLATION.md](../UV_INSTALLATION.md) for more details on using uv with PowerBI CLI.

### Display Issues

Ensure your terminal supports ANSI colors and has a reasonable size (at least 80x24 characters).

### Terminal Compatibility

The TUI works best with modern terminal emulators:
- **Linux**: GNOME Terminal, Konsole, Alacritty
- **macOS**: Terminal.app, iTerm2
- **Windows**: Windows Terminal, ConEmu

## Feedback and Issues

If you encounter any issues with the TUI, please report them on the [GitHub repository](https://github.com/emptymalei/powerbi-cli/issues).
