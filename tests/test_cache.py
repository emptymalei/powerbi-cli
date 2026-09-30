"""Tests for cache functionality."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from pbi_cli.cache import LAKE_FOLDER, CacheConfig, CacheManager


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


# ---------------------------------------------------------------------------
# The data lake lives in the cache folder and must survive the legacy cache commands
# ---------------------------------------------------------------------------


def _make_lake(cache_dir: Path) -> Path:
    lake = cache_dir / LAKE_FOLDER / "tenant=t" / "endpoint=admin_groups"
    lake.mkdir(parents=True)
    (lake / "keep.json").write_text("{}")
    return cache_dir / LAKE_FOLDER


def test_clearing_the_whole_cache_keeps_the_data_lake(temp_cache_dir):
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    manager.save("workspaces", {"value": []})
    manager.save("apps", {"value": []})
    (temp_cache_dir / "stray.txt").write_text("x")
    lake = _make_lake(temp_cache_dir)

    manager.clear()

    assert sorted(p.name for p in temp_cache_dir.iterdir()) == [LAKE_FOLDER]
    assert (lake / "tenant=t" / "endpoint=admin_groups" / "keep.json").exists()
    assert manager.list_keys() == []


def test_the_data_lake_cannot_be_cleared_as_a_cache_key(temp_cache_dir):
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    lake = _make_lake(temp_cache_dir)

    manager.clear(cache_key=LAKE_FOLDER)
    manager.clear(cache_key=LAKE_FOLDER, version="anything")

    assert (lake / "tenant=t" / "endpoint=admin_groups" / "keep.json").exists()


def test_the_data_lake_is_not_a_cache_key(temp_cache_dir):
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    manager.save("workspaces", {"value": []})
    _make_lake(temp_cache_dir)

    assert manager.list_keys() == ["workspaces"]


def test_clearing_one_key_leaves_the_lake_and_other_keys(temp_cache_dir):
    manager = CacheManager(cache_folder=str(temp_cache_dir))
    manager.save("workspaces", {"value": []})
    manager.save("apps", {"value": []})
    lake = _make_lake(temp_cache_dir)

    manager.clear(cache_key="workspaces")

    assert manager.list_keys() == ["apps"]
    assert lake.exists()


def test_clear_cache_command_keeps_the_lake_and_says_so(cache_folder):
    from typer.testing import CliRunner

    from pbi_cli.cli import app

    CacheManager(cache_folder=str(cache_folder)).save("workspaces", {"value": []})
    lake = _make_lake(cache_folder)

    result = CliRunner().invoke(app, ["cache", "clear", "--yes"])

    assert result.exit_code == 0
    assert "Cleared entire cache (the data lake was kept)" in result.output
    assert lake.exists()


def test_clear_cache_command_does_not_mention_a_lake_there_is_not(cache_folder):
    from typer.testing import CliRunner

    from pbi_cli.cli import app

    CacheManager(cache_folder=str(cache_folder)).save("workspaces", {"value": []})

    result = CliRunner().invoke(app, ["cache", "clear", "--yes"])

    assert result.exit_code == 0
    assert "Cleared entire cache" in result.output
    assert "data lake" not in result.output


def test_clear_cache_command_refuses_the_lake_as_a_key(cache_folder):
    from typer.testing import CliRunner

    from pbi_cli.cli import app

    lake = _make_lake(cache_folder)

    result = CliRunner().invoke(app, ["cache", "clear", "-k", LAKE_FOLDER, "--yes"])

    assert result.exit_code == 1
    assert "'lake' is the data lake, not a cache key" in result.output
    assert "pbi lake prune" in result.output
    assert lake.exists()


def test_list_cache_command_does_not_list_the_lake(cache_folder):
    from typer.testing import CliRunner

    from pbi_cli.cli import app

    CacheManager(cache_folder=str(cache_folder)).save("workspaces", {"value": []})
    _make_lake(cache_folder)

    result = CliRunner().invoke(app, ["cache", "list"])

    assert "workspaces (1 version(s))" in result.output
    assert "lake" not in result.output
