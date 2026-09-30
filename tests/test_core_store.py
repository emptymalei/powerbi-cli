"""Tests for the data lake store."""

import json
import os
import stat
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from cloudpathlib import CloudPath

from pbi_cli.core.store import LakeStore, StoreError, params_hash, safe_name

UTC = timezone.utc
T0 = datetime(2026, 9, 30, 12, 0, 0, 123456, tzinfo=UTC)
PARAMS = {"$expand": "reports,users", "$top": "1000"}


@pytest.fixture
def store(tmp_path):
    return LakeStore(tmp_path / "lake")


def write(
    store, data, *, at=T0, tenant="t1", endpoint="admin.groups", params=None, **kw
):
    return store.write_snapshot(
        tenant,
        endpoint,
        PARAMS if params is None else params,
        data,
        fetched_at=at,
        **kw,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_params_hash_is_stable_and_order_independent():
    assert params_hash({"a": "1", "b": "2"}) == params_hash({"b": "2", "a": "1"})
    assert params_hash({"a": "1"}) != params_hash({"a": "2"})
    assert params_hash({}) == params_hash({})
    # Pinned: changing the hash would orphan every snapshot already in a lake. It is the
    # first 12 hex digits of the SHA-256 of the canonical JSON: sorted keys, no spaces.
    assert params_hash({"$top": "1000"}) == "ab8d3b0ee9eb"


def test_safe_name():
    assert (
        safe_name("11111111-2222-3333-4444-555555555555")
        == "11111111-2222-3333-4444-555555555555"
    )
    assert safe_name("admin.groups") == "admin_groups"
    assert safe_name("a/b\\c:d") == "a_b_c_d"
    assert safe_name("") == "_"


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def test_nothing_is_created_until_the_first_write(tmp_path):
    root = tmp_path / "lake"
    store = LakeStore(root)
    assert not root.exists()
    assert store.latest("t1", "admin.groups", PARAMS) is None
    assert store.tenants() == []
    assert not root.exists()
    write(store, {"value": []})
    assert root.exists()


def test_snapshot_round_trip(store):
    data = {"value": [{"id": "w1", "name": "Finance éè"}]}
    snapshot = write(
        store,
        data,
        request={"method": "GET", "path": "/admin/groups", "params": PARAMS},
        profile="admin-nlm",
        pages=2,
        rows=1,
    )
    found = store.latest("t1", "admin.groups", PARAMS)
    assert found is not None
    assert found.load() == data
    assert found.directory == snapshot.directory
    assert found.fetched_at == T0
    assert found.version == "20260930T120000123456Z"

    manifest = found.manifest
    assert manifest["schema"] == 1
    assert manifest["kind"] == "snapshot"
    assert manifest["endpoint"] == "admin.groups"
    assert manifest["tenant"] == "t1"
    assert manifest["profile"] == "admin-nlm"
    assert manifest["params"] == PARAMS
    assert manifest["params_hash"] == params_hash(PARAMS)
    assert manifest["request"] == {
        "method": "GET",
        "path": "/admin/groups",
        "params": PARAMS,
    }
    assert (manifest["status"], manifest["pages"], manifest["rows"]) == (200, 2, 1)
    assert manifest["data_file"] == "data.json"
    assert manifest["cli_version"]


def test_manifest_holds_the_checksum_and_size_of_the_data(store):
    import hashlib

    snapshot = write(store, {"value": [1, 2, 3]})
    payload = (snapshot.directory / "data.json").read_bytes()
    assert snapshot.manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert snapshot.manifest["bytes"] == len(payload)


def test_layout_is_hive_style(store, tmp_path):
    snapshot = write(store, {"value": []})
    relative = snapshot.directory.relative_to(tmp_path / "lake")
    assert relative.parts == (
        "tenant=t1",
        "endpoint=admin_groups",
        f"params={params_hash(PARAMS)}",
        "dt=2026-09-30",
        "v=20260930T120000123456Z",
    )
    assert sorted(p.name for p in snapshot.directory.iterdir()) == [
        "data.json",
        "manifest.json",
    ]


def test_extra_manifest_fields_and_kind(store):
    snapshot = write(
        store, {"workspaces": []}, kind="job", extra={"workspace_ids": ["a", "b"]}
    )
    assert snapshot.manifest["kind"] == "job"
    assert snapshot.manifest["workspace_ids"] == ["a", "b"]


def test_latest_is_the_newest_across_days(store):
    write(store, {"n": 1}, at=T0)
    write(store, {"n": 3}, at=T0 + timedelta(days=1, hours=2))
    write(store, {"n": 2}, at=T0 + timedelta(hours=5))
    assert store.latest("t1", "admin.groups", PARAMS).load() == {"n": 3}
    assert [s.load()["n"] for s in store.versions("t1", "admin.groups", PARAMS)] == [
        3,
        2,
        1,
    ]


def test_requests_do_not_mix(store):
    write(store, {"which": "reports"}, params={"$expand": "reports"})
    write(store, {"which": "users"}, params={"$expand": "users"})
    write(store, {"which": "other tenant"}, tenant="t2", params={"$expand": "users"})
    write(
        store,
        {"which": "other endpoint"},
        endpoint="admin.apps",
        params={"$expand": "users"},
    )
    latest = store.latest("t1", "admin.groups", {"$expand": "users"})
    assert latest.load() == {"which": "users"}
    assert store.latest("t1", "admin.groups", {"$expand": "datasets"}) is None
    assert len(store.versions("t1", "admin.groups", {"$expand": "reports"})) == 1


def test_two_snapshots_at_the_same_instant_do_not_overwrite_each_other(store):
    first = write(store, {"n": 1})
    second = write(store, {"n": 2})
    assert first.directory != second.directory
    assert len(store.versions("t1", "admin.groups", PARAMS)) == 2
    assert store.latest("t1", "admin.groups", PARAMS).load() == {"n": 2}


def test_an_interrupted_write_is_ignored(store):
    good = write(store, {"n": 1}, at=T0)
    broken = write(store, {"n": 2}, at=T0 + timedelta(hours=1))
    (broken.directory / "manifest.json").unlink()  # the manifest is written last
    assert store.latest("t1", "admin.groups", PARAMS).directory == good.directory
    assert len(store.versions("t1", "admin.groups", PARAMS)) == 1


def test_an_unreadable_manifest_is_ignored(store):
    good = write(store, {"n": 1}, at=T0)
    broken = write(store, {"n": 2}, at=T0 + timedelta(hours=1))
    (broken.directory / "manifest.json").write_text("{nope", encoding="utf-8")
    assert store.latest("t1", "admin.groups", PARAMS).directory == good.directory


def test_writing_leaves_no_temporary_files(store):
    snapshot = write(store, {"n": 1})
    leftovers = [p for p in store.root.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []
    assert (snapshot.directory / "data.json").exists()


def test_the_request_record_must_not_hold_credentials(store):
    with pytest.raises(ValueError, match="must not hold"):
        write(
            store,
            {},
            request={"method": "GET", "headers": {"Authorization": "Bearer x"}},
        )
    with pytest.raises(ValueError, match="must not hold"):
        write(store, {}, request={"Authorization": "Bearer x"})
    with pytest.raises(ValueError, match="must not hold"):
        store.append_events(
            "t1", "admin.activityevents", date(2026, 9, 29), [], request={"token": "x"}
        )


def test_snapshot_age(store):
    snapshot = write(store, {}, at=T0)
    assert snapshot.age(now=T0 + timedelta(hours=3)) == timedelta(hours=3)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_files_and_the_lake_folder_are_private(store):
    snapshot = write(store, {"value": []})
    for name in ("data.json", "manifest.json"):
        mode = stat.S_IMODE(os.stat(snapshot.directory / name).st_mode)
        assert mode == 0o600, name
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700


def test_cloud_urls_give_a_cloud_root():
    store = LakeStore("s3://some-bucket/powerbi/lake")
    assert isinstance(store.root, CloudPath)
    assert str(store.root) == "s3://some-bucket/powerbi/lake"


# ---------------------------------------------------------------------------
# browsing
# ---------------------------------------------------------------------------


def test_browse_tenants_endpoints_and_parameter_sets(store):
    write(store, {"n": 1}, tenant="t1", endpoint="admin.groups", params={"$top": "5"})
    write(
        store,
        {"n": 2},
        tenant="t1",
        endpoint="admin.groups",
        params={"$top": "9"},
        at=T0 + timedelta(hours=1),
    )
    write(
        store,
        {"n": 3},
        tenant="t1",
        endpoint="admin.users.artifact_access",
        params={"userId": "u"},
    )
    write(store, {"n": 4}, tenant="t2", endpoint="admin.apps", params={})

    assert store.tenants() == ["t1", "t2"]
    assert store.endpoints("t1") == ["admin.groups", "admin.users.artifact_access"]
    assert store.endpoints("t2") == ["admin.apps"]
    assert store.endpoints("nobody") == []

    sets = store.parameter_sets("t1", "admin.groups")
    assert sorted(s.params["$top"] for s in sets) == ["5", "9"]
    by_top = {s.params["$top"]: s for s in sets}
    assert by_top["9"].latest.load() == {"n": 2}
    assert by_top["5"].hash == params_hash({"$top": "5"})
    assert store.parameter_sets("t1", "admin.nope") == []


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------


def test_prune_keeps_the_newest_versions_of_each_request(store):
    for hour in range(4):
        write(store, {"n": hour}, at=T0 + timedelta(hours=hour))
    write(store, {"n": 100}, params={"$top": "5"})

    assert store.prune(keep=2) == 2
    assert [s.load()["n"] for s in store.versions("t1", "admin.groups", PARAMS)] == [
        3,
        2,
    ]
    assert len(store.versions("t1", "admin.groups", {"$top": "5"})) == 1
    assert store.prune(keep=2) == 0


def test_prune_removes_empty_day_folders(store):
    old = write(store, {"n": 1}, at=T0)
    write(store, {"n": 2}, at=T0 + timedelta(days=2))
    store.prune(keep=1)
    assert not old.directory.parent.exists()  # dt=2026-09-30 is gone


def test_prune_can_be_limited_and_never_touches_unfinished_writes(store):
    for hour in range(3):
        write(store, {"n": hour}, at=T0 + timedelta(hours=hour))
    write(store, {"n": 9}, at=T0 + timedelta(hours=1), tenant="t2")
    unfinished = write(store, {"n": 7}, at=T0 + timedelta(hours=9))
    (unfinished.directory / "manifest.json").unlink()

    assert store.prune(keep=1, tenant="t1", endpoint_id="admin.apps") == 0
    assert store.prune(keep=1, tenant="t1") == 2
    assert len(store.versions("t2", "admin.groups", PARAMS)) == 1
    assert (unfinished.directory / "data.json").exists()


def test_prune_needs_a_positive_keep(store):
    with pytest.raises(ValueError):
        store.prune(keep=0)


def test_prune_leaves_event_logs_alone(store):
    store.append_events("t1", "admin.activityevents", date(2026, 9, 1), [{"Id": "a"}])
    write(store, {"n": 1})
    write(store, {"n": 2}, at=T0 + timedelta(hours=1))
    store.prune(keep=1)
    assert [
        e["Id"]
        for e in store.read_events("t1", "admin.activityevents", date(2026, 9, 1))
    ] == ["a"]


# ---------------------------------------------------------------------------
# event logs
# ---------------------------------------------------------------------------

DAY = date(2026, 9, 29)
EVENTS = "admin.activityevents"


def events(*ids):
    return [{"Id": i, "Activity": "ViewReport", "UserId": "u@contoso.com"} for i in ids]


def test_append_and_read_events_in_order(store):
    assert store.append_events("t1", EVENTS, DAY, events("a", "b", "c")) == 3
    assert [e["Id"] for e in store.read_events("t1", EVENTS, DAY)] == ["a", "b", "c"]
    day = store.event_day("t1", EVENTS, DAY)
    assert (day.rows, day.sealed, day.cursor, day.day) == (3, False, None, DAY)
    assert day.manifest["parts"] == ["part-0000.jsonl"]
    assert day.manifest["kind"] == "events"


def test_events_already_stored_are_skipped(store):
    store.append_events("t1", EVENTS, DAY, events("a", "b"))
    assert store.append_events("t1", EVENTS, DAY, events("b", "c", "a", "d")) == 2
    assert [e["Id"] for e in store.read_events("t1", EVENTS, DAY)] == [
        "a",
        "b",
        "c",
        "d",
    ]
    day = store.event_day("t1", EVENTS, DAY)
    assert day.rows == 4
    assert day.manifest["parts"] == ["part-0000.jsonl", "part-0001.jsonl"]


def test_duplicates_inside_one_batch_are_skipped(store):
    assert store.append_events("t1", EVENTS, DAY, events("a", "a", "b")) == 2


def test_an_empty_append_writes_no_part(store):
    store.append_events("t1", EVENTS, DAY, events("a"))
    assert store.append_events("t1", EVENTS, DAY, events("a")) == 0
    assert store.event_day("t1", EVENTS, DAY).manifest["parts"] == ["part-0000.jsonl"]


def test_events_without_an_id_are_always_added(store):
    store.append_events("t1", EVENTS, DAY, [{"Activity": "x"}])
    assert store.append_events("t1", EVENTS, DAY, [{"Activity": "x"}]) == 1
    assert store.event_day("t1", EVENTS, DAY).rows == 2


def test_a_different_id_field(store):
    store.append_events("t1", "x", DAY, [{"key": 1}], id_field="key")
    assert (
        store.append_events("t1", "x", DAY, [{"key": 1}, {"key": 2}], id_field="key")
        == 1
    )


def test_the_cursor_is_kept_until_replaced(store):
    store.append_events("t1", EVENTS, DAY, events("a"), cursor="token-1")
    assert store.event_day("t1", EVENTS, DAY).cursor == "token-1"
    store.append_events("t1", EVENTS, DAY, events("b"))  # no cursor given: keep it
    assert store.event_day("t1", EVENTS, DAY).cursor == "token-1"
    store.append_events("t1", EVENTS, DAY, events("c"), cursor="token-2")
    assert store.event_day("t1", EVENTS, DAY).cursor == "token-2"


def test_a_sealed_day_takes_no_more_events(store):
    store.append_events("t1", EVENTS, DAY, events("a"), sealed=True)
    assert store.event_day("t1", EVENTS, DAY).sealed
    with pytest.raises(StoreError, match="sealed"):
        store.append_events("t1", EVENTS, DAY, events("b"))
    assert [e["Id"] for e in store.read_events("t1", EVENTS, DAY)] == ["a"]


def test_seal_day(store):
    store.append_events("t1", EVENTS, DAY, events("a"))
    store.seal_day("t1", EVENTS, DAY)
    assert store.event_day("t1", EVENTS, DAY).sealed
    with pytest.raises(StoreError):
        store.seal_day("t1", EVENTS, date(2026, 1, 1))


def test_event_days_are_listed_newest_first(store):
    for day in (date(2026, 9, 27), date(2026, 9, 29), date(2026, 9, 28)):
        store.append_events("t1", EVENTS, day, events(day.isoformat()))
    assert [d.day for d in store.event_days("t1", EVENTS)] == [
        date(2026, 9, 29),
        date(2026, 9, 28),
        date(2026, 9, 27),
    ]
    assert store.event_days("t1", "admin.nope") == []
    assert store.event_day("t1", EVENTS, date(2020, 1, 1)) is None
    assert list(store.read_events("t1", EVENTS, date(2020, 1, 1))) == []


def test_event_files_are_jsonl_with_unicode_intact(store):
    store.append_events(
        "t1", EVENTS, DAY, [{"Id": "a", "ItemName": "Bilanz über 2026"}]
    )
    day = store.event_day("t1", EVENTS, DAY)
    lines = (day.directory / "part-0000.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["ItemName"] == "Bilanz über 2026"
    assert len(lines) == 1


def test_events_show_up_when_browsing(store):
    store.append_events("t1", EVENTS, DAY, events("a"))
    assert store.tenants() == ["t1"]
    assert store.endpoints("t1") == [EVENTS]
