"""Configuration management for pbi-cli using YAML."""

import json
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from loguru import logger

# Public API
__all__ = [
    "PBIConfig",
    "resolve_output_path",
    "migrate_legacy_config",
]

CONFIG_DIR = Path.home() / ".pbi_cli"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Legacy files for migration
LEGACY_AUTH_CONFIG_FILE = CONFIG_DIR / "auth.json"
LEGACY_PROFILES_FILE = CONFIG_DIR / "profiles.json"


class PBIConfig:
    """Configuration manager for pbi-cli.

    This class provides a convenient interface for managing pbi-cli configuration,
    including profiles, default output folder, and other settings.

    Example usage:
        config = PBIConfig()
        config.default_output_folder = "/path/to/backups"
        print(config.active_profile)
        config.set("custom.setting", "value")
    """

    def __init__(self, config_file: Optional[Path] = None):
        """Initialize the config manager.

        :param config_file: Optional path to config file (defaults to ~/.pbi_cli/config.yaml)
        """
        self._config_file = config_file or CONFIG_FILE
        self._config_dir = self._config_file.parent
        self._data: Optional[Dict] = None

    def _ensure_config_dir(self):
        """Ensure the config directory exists."""
        if not self._config_dir.exists():
            self._config_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        """Load configuration from YAML file.

        Returns default config if file doesn't exist.
        """
        if not self._config_file.exists():
            return self._get_default_config()

        try:
            with open(self._config_file, "r", encoding="utf-8") as fp:
                config = yaml.safe_load(fp)
                # Only return default if config is None (empty file) or not a dict
                if config is None or not isinstance(config, dict):
                    return self._get_default_config()
                return config
        except Exception as e:
            logger.warning(f"Could not load config from {self._config_file}: {e}")
            return self._get_default_config()

    def _save(self, config: dict):
        """Save configuration to YAML file."""
        self._ensure_config_dir()
        with open(self._config_file, "w", encoding="utf-8") as fp:
            yaml.dump(config, fp, default_flow_style=False, sort_keys=False)
        self._data = None  # Invalidate cache

    @staticmethod
    def _get_default_config() -> dict:
        """Get default configuration."""
        return {
            "active_profile": None,
            "profiles": {},
            "default_output_folder": None,
        }

    @property
    def data(self) -> dict:
        """Get the raw configuration data.

        :return: Configuration dictionary
        """
        if self._data is None:
            self._data = self._load()
        return self._data

    def reload(self):
        """Reload configuration from file."""
        self._data = None

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by key.

        :param key: Configuration key (supports nested keys with dot notation)
        :param default: Default value if key not found
        :return: Configuration value or default
        """
        config = self.data

        # Support nested keys like "profiles.default.name"
        keys = key.split(".")
        value = config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any):
        """Set a configuration value.

        :param key: Configuration key (supports nested keys with dot notation)
        :param value: Value to set
        """
        config = self.data.copy()

        # Support nested keys like "profiles.default.name"
        keys = key.split(".")
        current = config
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            elif not isinstance(current[k], dict):
                # If the intermediate key exists but is not a dict, we can't traverse further
                current_type = type(current[k]).__name__
                raise ValueError(
                    f"Cannot set nested key '{key}': '{k}' is a {current_type}, not a dictionary"
                )
            current = current[k]

        current[keys[-1]] = value
        self._save(config)

    # Commonly used properties for easy access

    @property
    def active_profile(self) -> Optional[str]:
        """Get the active profile name.

        :return: Active profile name or None
        """
        return self.get("active_profile")

    @active_profile.setter
    def active_profile(self, value: Optional[str]):
        """Set the active profile name.

        :param value: Profile name or None
        """
        self.set("active_profile", value)

    @property
    def profiles(self) -> dict:
        """Get all profiles.

        :return: Dictionary of profiles
        """
        return self.get("profiles", {})

    @profiles.setter
    def profiles(self, value: dict):
        """Set all profiles.

        :param value: Dictionary of profiles
        """
        self.set("profiles", value)

    @property
    def default_output_folder(self) -> Optional[str]:
        """Get the default output folder.

        :return: Default output folder path or None
        """
        return self.get("default_output_folder")

    @default_output_folder.setter
    def default_output_folder(self, value: Optional[str]):
        """Set the default output folder.

        Handles Windows paths with backslashes and shell escaping issues by:
        1. Converting to raw string representation
        2. Stripping any escaped quotes
        3. Normalizing the path using pathlib

        :param value: Path to the default output folder
        """
        if value is not None:
            # Handle shell escaping issues: if value ends with \", the quote is escaped
            # We need to strip trailing quotes that were escaped by backslashes
            # Use repr/eval to handle raw string conversion safely
            try:
                # If the string ends with an escaped quote, remove it
                if value.endswith('\\"') or value.endswith("\\'"):
                    value = value[:-1]  # Remove the escaped quote
                elif value.endswith('"') or value.endswith("'"):
                    # Check if it's a legitimate quote or an escaped one
                    if len(value) > 1 and value[-2] != "\\":
                        value = value[:-1]  # Strip unescaped trailing quote
            except Exception:
                pass  # If anything goes wrong, use the value as-is

            # Strip leading quotes if present
            if (value.startswith('"') and not value.startswith('\\"')) or (
                value.startswith("'") and not value.startswith("\\'")
            ):
                value = value[1:]

            # Expand user home directory if needed and convert to absolute path
            # pathlib handles path normalization across platforms
            path = Path(value).expanduser().absolute()
            self.set("default_output_folder", str(path))
        else:
            self.set("default_output_folder", None)

    @property
    def config_dir(self) -> Path:
        """Get the configuration directory path.

        :return: Path to config directory
        """
        return self._config_dir

    @property
    def config_file(self) -> Path:
        """Get the configuration file path.

        :return: Path to config file
        """
        return self._config_file

    def has_profile(self, profile_name: str) -> bool:
        """Check if a profile exists.

        :param profile_name: Profile name to check
        :return: True if profile exists, False otherwise
        """
        return profile_name in self.profiles

    def add_profile(self, profile_name: str, profile_data: Optional[dict] = None):
        """Add a new profile.

        :param profile_name: Name of the profile
        :param profile_data: Optional profile data dictionary
        """
        profiles = self.profiles.copy()
        profiles[profile_name] = profile_data or {"name": profile_name}
        self.profiles = profiles

    def remove_profile(self, profile_name: str):
        """Remove a profile.

        :param profile_name: Name of the profile to remove
        """
        profiles = self.profiles.copy()
        if profile_name in profiles:
            del profiles[profile_name]
            self.profiles = profiles

            # If this was the active profile, clear it
            if self.active_profile == profile_name:
                # Set to first remaining profile or None
                remaining = list(profiles.keys())
                self.active_profile = remaining[0] if remaining else None


# Module-level functions for backward compatibility and convenience


def ensure_config_dir():
    """Ensure the config directory exists."""
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_default_config() -> dict:
    """Get default configuration."""
    return PBIConfig._get_default_config()


def migrate_legacy_config():
    """Migrate legacy JSON config files to YAML format."""
    if CONFIG_FILE.exists():
        # Already migrated
        return

    pbi_config = PBIConfig()
    migrated = False

    # Migrate profiles.json if it exists
    if LEGACY_PROFILES_FILE.exists():
        try:
            with open(LEGACY_PROFILES_FILE, "r", encoding="utf-8") as fp:
                profiles_data = json.load(fp)
                pbi_config.active_profile = profiles_data.get("active_profile")
                pbi_config.profiles = profiles_data.get("profiles", {})
                migrated = True
                logger.info("Migrated profiles from JSON to YAML")
        except Exception as e:
            logger.warning(f"Could not migrate profiles.json: {e}")

    # No explicit save needed - the properties handle it


def resolve_output_path(path_input: Optional[str]) -> Optional[Path]:
    """Resolve output path from user input.

    If path_input is None, returns None.
    If path_input is an absolute path, returns it as-is.
    If path_input is a relative path, combines it with default_output_folder from config.

    :param path_input: User-provided path (can be absolute or relative)
    :return: Resolved Path object or None
    """
    if path_input is None:
        return None

    path = Path(path_input)

    # If it's an absolute path, return as-is
    if path.is_absolute():
        return path

    # If it's a relative path, combine with default_output_folder
    config = PBIConfig()
    default_output_folder = config.default_output_folder

    if default_output_folder:
        base_path = Path(default_output_folder)
        return base_path / path
    else:
        # If no default_output_folder is set, treat as relative to current directory
        return Path.cwd() / path
