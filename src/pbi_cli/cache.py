"""Cache management for PowerBI CLI.

This module provides caching functionality for API call results,
supporting both local and remote storage (e.g., S3) using cloudpathlib.

The cache system:
- Stores data in JSON format
- Uses timestamp-based versioning (subfolders)
- Supports both local and remote paths (S3, etc.)
- Designed for extensibility and analysis

Example:
    >>> from pbi_cli.cache import CacheManager
    >>> cache = CacheManager()
    >>> cache.save("workspaces", {"value": [{"id": "123"}]})
    >>> data = cache.load("workspaces", version="latest")
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from cloudpathlib import AnyPath, CloudPath
from loguru import logger

__all__ = ["CacheManager", "CacheConfig"]


class CacheConfig:
    """Configuration for cache management.
    
    Attributes:
        cache_folder: Base folder for cache storage (local or remote path)
        enabled: Whether caching is enabled
        default_versioning: Whether to use timestamp versioning by default
    """

    def __init__(
        self,
        cache_folder: Union[str, Path, CloudPath, None] = None,
        enabled: bool = True,
        default_versioning: bool = True,
    ):
        """Initialize cache configuration.
        
        :param cache_folder: Base folder for cache storage
        :param enabled: Whether caching is enabled
        :param default_versioning: Whether to use timestamp versioning by default
        """
        self.cache_folder = cache_folder
        self.enabled = enabled
        self.default_versioning = default_versioning

    @property
    def cache_path(self) -> Optional[AnyPath]:
        """Get the cache path as an AnyPath object.
        
        :return: AnyPath object or None if not configured
        """
        if self.cache_folder is None:
            return None
        return AnyPath(self.cache_folder)


class CacheManager:
    """Manager for cache operations with versioning support.
    
    This class handles saving, loading, and managing cached API results.
    It supports timestamp-based versioning and works with both local and
    remote storage through cloudpathlib.
    
    Example:
        >>> cache = CacheManager(cache_folder="/path/to/cache")
        >>> cache.save("workspaces", {"value": [{"id": "123"}]})
        >>> data = cache.load("workspaces", version="latest")
    """

    def __init__(
        self,
        cache_folder: Union[str, Path, CloudPath, None] = None,
        config: Optional[CacheConfig] = None,
    ):
        """Initialize the cache manager.
        
        :param cache_folder: Base folder for cache storage (overrides config)
        :param config: CacheConfig object (optional)
        """
        if config is None:
            config = CacheConfig(cache_folder=cache_folder)
        elif cache_folder is not None:
            # Override config's cache_folder if explicitly provided
            config.cache_folder = cache_folder

        self.config = config

    @property
    def _base_path(self) -> Optional[AnyPath]:
        """Get the base cache path.
        
        :return: AnyPath object or None if not configured
        """
        return self.config.cache_path

    def _ensure_cache_dir(self, path: AnyPath):
        """Ensure the cache directory exists.
        
        :param path: Path to ensure exists
        """
        if not isinstance(path, CloudPath):
            # For local paths, create directory
            Path(str(path)).mkdir(parents=True, exist_ok=True)
        # For cloud paths, directories are created automatically on write

    def _get_version_timestamp(self) -> str:
        """Generate a timestamp string for versioning.
        
        :return: Timestamp string in ISO format
        """
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _get_cache_path(
        self, cache_key: str, version: Optional[str] = None, create_version: bool = False
    ) -> Optional[AnyPath]:
        """Get the full cache path for a given key and version.
        
        :param cache_key: Key identifying the cached data
        :param version: Version identifier (timestamp or "latest")
        :param create_version: Whether to create a new version timestamp
        :return: Full path to the cache file or None if cache not configured
        """
        if self._base_path is None:
            return None

        if create_version or (version is None and self.config.default_versioning):
            # Create new version with timestamp
            version = self._get_version_timestamp()

        if version:
            # Versioned cache: base_path / cache_key / version / cache_key.json
            cache_dir = self._base_path / cache_key / version
            cache_file = cache_dir / f"{cache_key}.json"
        else:
            # Non-versioned cache: base_path / cache_key.json
            cache_file = self._base_path / f"{cache_key}.json"

        return cache_file

    def save(
        self,
        cache_key: str,
        data: Any,
        version: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Save data to cache with optional versioning.
        
        :param cache_key: Key identifying the cached data
        :param data: Data to cache (must be JSON serializable)
        :param version: Optional version identifier (auto-generated if None)
        :param metadata: Optional metadata to include in cache
        :return: Version identifier used, or None if cache is disabled/not configured
        """
        if not self.config.enabled or self._base_path is None:
            logger.debug("Cache is disabled or not configured")
            return None

        try:
            cache_path = self._get_cache_path(cache_key, version, create_version=True)
            if cache_path is None:
                return None

            # Extract version from path
            if self.config.default_versioning:
                # For versioned cache: base/key/version/key.json
                used_version = cache_path.parent.name
            else:
                # For non-versioned cache, use timestamp as version
                used_version = self._get_version_timestamp()

            # Prepare cache data with metadata
            cache_data = {
                "cache_key": cache_key,
                "cached_at": datetime.now().isoformat(),
                "version": used_version,
                "metadata": metadata or {},
                "data": data,
            }

            # Ensure directory exists
            self._ensure_cache_dir(cache_path.parent)

            # Write cache file
            with cache_path.open("w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)

            logger.info(f"Cached data to {cache_path}")
            return used_version

        except Exception as e:
            logger.warning(f"Failed to save cache for {cache_key}: {e}")
            return None

    def load(
        self, cache_key: str, version: Optional[str] = "latest"
    ) -> Optional[Dict[str, Any]]:
        """Load data from cache.
        
        :param cache_key: Key identifying the cached data
        :param version: Version to load ("latest" for most recent, None for non-versioned)
        :return: Cached data dictionary or None if not found
        """
        if not self.config.enabled or self._base_path is None:
            logger.debug("Cache is disabled or not configured")
            return None

        try:
            if version == "latest":
                # Find the latest version
                versions = self.list_versions(cache_key)
                if not versions:
                    logger.debug(f"No cached versions found for {cache_key}")
                    return None
                version = versions[0]  # Most recent version

            cache_path = self._get_cache_path(cache_key, version)
            if cache_path is None or not cache_path.exists():
                logger.debug(f"Cache file not found: {cache_path}")
                return None

            # Read cache file
            with cache_path.open("r", encoding="utf-8") as f:
                cache_data = json.load(f)

            logger.info(f"Loaded cache from {cache_path}")
            return cache_data

        except Exception as e:
            logger.warning(f"Failed to load cache for {cache_key}: {e}")
            return None

    def list_versions(self, cache_key: str) -> List[str]:
        """List all available versions for a cache key.
        
        Versions are returned in descending order (most recent first).
        
        :param cache_key: Key identifying the cached data
        :return: List of version identifiers
        """
        if self._base_path is None:
            return []

        try:
            cache_dir = self._base_path / cache_key

            if not cache_dir.exists():
                return []

            # List all subdirectories (versions)
            versions = []
            for item in cache_dir.iterdir():
                if item.is_dir():
                    # Check if this directory contains the cache file
                    cache_file = item / f"{cache_key}.json"
                    if cache_file.exists():
                        versions.append(item.name)

            # Sort versions in descending order (most recent first)
            return sorted(versions, reverse=True)

        except Exception as e:
            logger.warning(f"Failed to list versions for {cache_key}: {e}")
            return []

    def list_keys(self) -> List[str]:
        """List all cache keys.
        
        :return: List of cache keys
        """
        if self._base_path is None:
            return []

        try:
            if not self._base_path.exists():
                return []

            keys = []
            for item in self._base_path.iterdir():
                if item.is_dir():
                    # Check if this is a cache key directory
                    versions = self.list_versions(item.name)
                    if versions:
                        keys.append(item.name)

            return sorted(keys)

        except Exception as e:
            logger.warning(f"Failed to list cache keys: {e}")
            return []

    def clear(self, cache_key: Optional[str] = None, version: Optional[str] = None):
        """Clear cache data.
        
        :param cache_key: Specific cache key to clear (clears all if None)
        :param version: Specific version to clear (clears all versions if None)
        """
        if self._base_path is None:
            logger.warning("Cache path not configured")
            return

        try:
            if cache_key is None:
                # Clear entire cache
                if self._base_path.exists():
                    import shutil

                    if isinstance(self._base_path, CloudPath):
                        # For cloud paths, remove all objects
                        for item in self._base_path.iterdir():
                            if item.is_dir():
                                item.rmtree()
                            else:
                                item.unlink()
                    else:
                        # For local paths
                        shutil.rmtree(str(self._base_path))
                        self._base_path.mkdir(parents=True, exist_ok=True)
                    logger.info("Cleared entire cache")
            elif version is None:
                # Clear all versions of a specific key
                cache_dir = self._base_path / cache_key
                if cache_dir.exists():
                    import shutil

                    if isinstance(cache_dir, CloudPath):
                        cache_dir.rmtree()
                    else:
                        shutil.rmtree(str(cache_dir))
                    logger.info(f"Cleared all versions of {cache_key}")
            else:
                # Clear specific version
                cache_path = self._get_cache_path(cache_key, version)
                if cache_path and cache_path.exists():
                    cache_path.unlink()
                    logger.info(f"Cleared {cache_key} version {version}")

        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
