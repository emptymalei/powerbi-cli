"""The lakes that were opened lately are remembered, for the TUI to offer again."""

import pytest

from pbi_cli.config import PBIConfig


def test_there_are_no_recent_lakes_at_first(isolated_home):
    assert PBIConfig().recent_lakes == []


def test_the_newest_lake_is_first_and_each_is_listed_once(isolated_home):
    config = PBIConfig()
    for place in ("a", "b", "c", "a"):
        config.remember_lake(place)

    assert PBIConfig().recent_lakes == ["a", "c", "b"]


def test_only_the_last_few_are_kept(isolated_home):
    config = PBIConfig()
    for number in range(PBIConfig.RECENT_LAKES + 4):
        config.remember_lake(f"s3://bucket/{number}")

    kept = PBIConfig().recent_lakes
    assert len(kept) == PBIConfig.RECENT_LAKES
    assert kept[0] == f"s3://bucket/{PBIConfig.RECENT_LAKES + 3}"
    assert "s3://bucket/0" not in kept


@pytest.mark.parametrize("junk", ["text", 5, {"a": 1}, None])
def test_a_damaged_list_is_ignored(isolated_home, junk):
    PBIConfig().set("recent_lakes", junk)

    assert PBIConfig().recent_lakes == []
    PBIConfig().remember_lake("a")
    assert PBIConfig().recent_lakes == ["a"]
