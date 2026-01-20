"""Tests for cache functionality."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from pbi_cli.cache import CacheConfig, CacheManager


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    # Cleanup
    shutil.rmtree(temp_dir)


def test_cache_config_initialization():
    """Test CacheConfig initialization."""
    config = CacheConfig(cache_folder="/tmp/cache", enabled=True)
    assert config.cache_folder == "/tmp/cache"
    assert config.enabled is True
    assert config.cache_path.name == "cache"


def test_cache_manager_initialization(temp_cache_dir):
    """Test CacheManager initialization."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    assert manager.config.cache_folder == str(temp_cache_dir)


def test_cache_save_and_load(temp_cache_dir):
    """Test saving and loading cache data."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    # Save data
    test_data = {"value": [{"id": "123", "name": "Test"}]}
    version = manager.save("test_key", test_data)
    
    assert version is not None
    
    # Load data
    loaded = manager.load("test_key", version="latest")
    
    assert loaded is not None
    assert loaded["cache_key"] == "test_key"
    assert loaded["data"] == test_data


def test_cache_versioning(temp_cache_dir):
    """Test cache versioning."""
    import time
    
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    # Save multiple versions
    v1 = manager.save("test_key", {"version": 1})
    time.sleep(1)  # Ensure different timestamps
    v2 = manager.save("test_key", {"version": 2})
    
    assert v1 != v2
    
    # List versions
    versions = manager.list_versions("test_key")
    assert len(versions) == 2
    assert v2 in versions
    assert v1 in versions
    
    # Load latest
    latest = manager.load("test_key", version="latest")
    assert latest["data"]["version"] == 2


def test_cache_with_metadata(temp_cache_dir):
    """Test saving cache with metadata."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    test_data = {"value": [{"id": "123"}]}
    metadata = {"top": 1000, "expand": ["users"]}
    
    version = manager.save("test_key", test_data, metadata=metadata)
    loaded = manager.load("test_key", version=version)
    
    assert loaded["metadata"] == metadata


def test_cache_list_keys(temp_cache_dir):
    """Test listing cache keys."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    manager.save("key1", {"data": 1})
    manager.save("key2", {"data": 2})
    
    keys = manager.list_keys()
    assert "key1" in keys
    assert "key2" in keys


def test_cache_clear_all(temp_cache_dir):
    """Test clearing entire cache."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    manager.save("key1", {"data": 1})
    manager.save("key2", {"data": 2})
    
    manager.clear()
    
    keys = manager.list_keys()
    assert len(keys) == 0


def test_cache_clear_specific_key(temp_cache_dir):
    """Test clearing specific cache key."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    manager.save("key1", {"data": 1})
    manager.save("key2", {"data": 2})
    
    manager.clear(cache_key="key1")
    
    keys = manager.list_keys()
    assert "key1" not in keys
    assert "key2" in keys


def test_cache_disabled(temp_cache_dir):
    """Test cache when disabled."""
    config = CacheConfig(cache_folder=str(temp_cache_dir), enabled=False)
    manager = CacheManager(config=config)
    
    version = manager.save("test_key", {"data": 1})
    assert version is None
    
    loaded = manager.load("test_key")
    assert loaded is None


def test_cache_not_configured():
    """Test cache when not configured."""
    manager = CacheManager(cache_folder=None)
    
    version = manager.save("test_key", {"data": 1})
    assert version is None
    
    loaded = manager.load("test_key")
    assert loaded is None


def test_cache_structure(temp_cache_dir):
    """Test the cache directory structure."""
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    
    version = manager.save("workspaces", {"value": [{"id": "123"}]})
    
    # Check structure: cache_folder/workspaces/version/workspaces.json
    cache_file = temp_cache_dir / "workspaces" / version / "workspaces.json"
    assert cache_file.exists()
    
    # Verify JSON structure
    with open(cache_file, "r") as f:
        data = json.load(f)
        assert "cache_key" in data
        assert "cached_at" in data
        assert "version" in data
        assert "data" in data
        assert "metadata" in data
