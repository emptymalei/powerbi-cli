"""Replacing a file, also where the platform refuses while a reader has it open."""

import os

import pytest
from cloudpathlib import AnyPath

from pbi_cli.core import fsutil
from pbi_cli.core.fsutil import ATTEMPTS, PAUSE, inside_place, replace_file, same_place


def refusing(times, error=PermissionError("in use")):
    """An `os.replace` that fails ``times`` times, then works."""
    real = os.replace
    calls = []

    def replace(source, target):
        calls.append((source, target))
        if len(calls) <= times:
            raise error
        real(source, target)

    replace.calls = calls
    return replace


@pytest.fixture
def files(tmp_path):
    source, target = tmp_path / "new.tmp", tmp_path / "data.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    return source, target


def test_a_file_replaces_the_one_at_the_target(files):
    source, target = files

    replace_file(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_a_file_in_use_is_tried_again_with_growing_pauses_on_windows(
    files, monkeypatch
):
    source, target = files
    monkeypatch.setattr(fsutil, "RETRY_REPLACE", True)
    monkeypatch.setattr(os, "replace", refusing(2))
    pauses = []

    replace_file(source, target, sleep=pauses.append)

    assert target.read_text(encoding="utf-8") == "new"
    assert pauses == [PAUSE, PAUSE * 2]


def test_it_gives_up_after_the_attempts_and_lets_the_error_through(files, monkeypatch):
    source, target = files
    monkeypatch.setattr(fsutil, "RETRY_REPLACE", True)
    always = refusing(10_000)
    monkeypatch.setattr(os, "replace", always)
    pauses = []

    with pytest.raises(PermissionError):
        replace_file(source, target, sleep=pauses.append)

    assert len(always.calls) == ATTEMPTS
    assert len(pauses) == ATTEMPTS - 1
    assert target.read_text(encoding="utf-8") == "old"


def test_elsewhere_a_permission_error_is_a_real_one_and_is_raised_at_once(
    files, monkeypatch
):
    source, target = files
    monkeypatch.setattr(fsutil, "RETRY_REPLACE", False)
    monkeypatch.setattr(os, "replace", refusing(1))
    pauses = []

    with pytest.raises(PermissionError):
        replace_file(source, target, sleep=pauses.append)

    assert pauses == []


def test_other_errors_are_never_tried_again(files, monkeypatch):
    source, target = files
    monkeypatch.setattr(fsutil, "RETRY_REPLACE", True)
    missing = refusing(1, FileNotFoundError("gone"))
    monkeypatch.setattr(os, "replace", missing)
    pauses = []

    with pytest.raises(FileNotFoundError):
        replace_file(source, target, sleep=pauses.append)

    assert len(missing.calls) == 1 and pauses == []


# ---------------------------------------------------------------------------
# places
# ---------------------------------------------------------------------------


def test_the_same_folder_is_the_same_place_however_it_is_written(tmp_path):
    assert same_place(tmp_path / "a" / ".." / "b", tmp_path / "b")
    assert same_place(str(tmp_path / "b"), tmp_path / "b")
    assert not same_place(tmp_path / "b", tmp_path / "c")


def test_a_home_folder_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert same_place("~/lake", tmp_path / "lake")


def test_a_cloud_url_is_the_same_place_with_or_without_the_closing_slash(local_s3):
    assert same_place(AnyPath("s3://bucket/x/"), AnyPath("s3://bucket/x"))
    assert not same_place(AnyPath("s3://bucket/x"), AnyPath("s3://bucket/y"))
    assert not same_place(AnyPath("s3://bucket/x"), AnyPath("s3://other/x"))


def test_a_cloud_url_is_not_a_folder_on_disk(local_s3, tmp_path):
    assert not same_place(AnyPath("s3://bucket/x"), tmp_path / "x")


def test_a_place_is_inside_another_when_it_is_below_it_or_the_same(tmp_path):
    assert inside_place(tmp_path / "a" / "b", tmp_path / "a")
    assert inside_place(tmp_path / "a", tmp_path / "a")
    assert not inside_place(tmp_path / "a", tmp_path / "a" / "b")
    assert not inside_place(tmp_path / "ab", tmp_path / "a")  # not a prefix of the name


def test_inside_works_for_cloud_urls_too(local_s3):
    assert inside_place(AnyPath("s3://bucket/x/y"), AnyPath("s3://bucket/x"))
    assert inside_place(AnyPath("s3://bucket/x/"), AnyPath("s3://bucket/x"))
    assert not inside_place(AnyPath("s3://bucket/xy"), AnyPath("s3://bucket/x"))
    assert not inside_place(AnyPath("s3://bucket/x"), AnyPath("s3://bucket/x/y"))
