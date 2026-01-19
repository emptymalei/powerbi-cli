"""Configuration management for pbi-cli using YAML."""
import json
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

CONFIG_DIR = Path.home() / ".pbi_cli"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Legacy files for migration
LEGACY_AUTH_CONFIG_FILE = CONFIG_DIR / "auth.json"
LEGACY_PROFILES_FILE = CONFIG_DIR / "profiles.json"


def ensure_config_dir():
    """Ensure the config directory exists."""
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load configuration from YAML file.
    
    Returns default config if file doesn't exist.
    """
    if not CONFIG_FILE.exists():
        return get_default_config()
    
    try:
        with open(CONFIG_FILE, "r") as fp:
            config = yaml.safe_load(fp)
            return config or get_default_config()
    except Exception as e:
        logger.warning(f"Could not load config from {CONFIG_FILE}: {e}")
        return get_default_config()


def save_config(config: dict):
    """Save configuration to YAML file."""
    ensure_config_dir()
    with open(CONFIG_FILE, "w") as fp:
        yaml.dump(config, fp, default_flow_style=False, sort_keys=False)


def get_default_config() -> dict:
    """Get default configuration."""
    return {
        "active_profile": None,
        "profiles": {},
        "default_output_folder": None,
    }


def get_config_value(key: str, default: Any = None) -> Any:
    """Get a configuration value by key.
    
    :param key: Configuration key (supports nested keys with dot notation)
    :param default: Default value if key not found
    :return: Configuration value or default
    """
    config = load_config()
    
    # Support nested keys like "profiles.default.name"
    keys = key.split(".")
    value = config
    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default
    
    return value


def set_config_value(key: str, value: Any):
    """Set a configuration value.
    
    :param key: Configuration key (supports nested keys with dot notation)
    :param value: Value to set
    """
    config = load_config()
    
    # Support nested keys like "profiles.default.name"
    keys = key.split(".")
    current = config
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        current = current[k]
    
    current[keys[-1]] = value
    save_config(config)


def migrate_legacy_config():
    """Migrate legacy JSON config files to YAML format."""
    if CONFIG_FILE.exists():
        # Already migrated
        return
    
    config = get_default_config()
    migrated = False
    
    # Migrate profiles.json if it exists
    if LEGACY_PROFILES_FILE.exists():
        try:
            with open(LEGACY_PROFILES_FILE, "r") as fp:
                profiles_data = json.load(fp)
                config["active_profile"] = profiles_data.get("active_profile")
                config["profiles"] = profiles_data.get("profiles", {})
                migrated = True
                logger.info("Migrated profiles from JSON to YAML")
        except Exception as e:
            logger.warning(f"Could not migrate profiles.json: {e}")
    
    if migrated:
        save_config(config)


def resolve_output_path(path_input: Optional[str], default_subfolder: str = "") -> Optional[Path]:
    """Resolve output path from user input.
    
    If path_input is None, returns None.
    If path_input is an absolute path, returns it as-is.
    If path_input is a relative path, combines it with default_output_folder from config.
    
    :param path_input: User-provided path (can be absolute or relative)
    :param default_subfolder: Default subfolder name if no path provided
    :return: Resolved Path object or None
    """
    if path_input is None:
        return None
    
    path = Path(path_input)
    
    # If it's an absolute path, return as-is
    if path.is_absolute():
        return path
    
    # If it's a relative path, combine with default_output_folder
    default_output_folder = get_config_value("default_output_folder")
    
    if default_output_folder:
        base_path = Path(default_output_folder)
        return base_path / path
    else:
        # If no default_output_folder is set, treat as relative to current directory
        return Path.cwd() / path


def get_default_output_folder() -> Optional[str]:
    """Get the default output folder from config."""
    return get_config_value("default_output_folder")


def set_default_output_folder(folder_path: str):
    """Set the default output folder in config.
    
    :param folder_path: Path to the default output folder
    """
    # Expand user home directory if needed
    path = Path(folder_path).expanduser()
    set_config_value("default_output_folder", str(path.absolute()))
