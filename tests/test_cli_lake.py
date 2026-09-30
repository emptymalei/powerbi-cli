"""``pbi lake ls | show | prune`` work on a lake that the commands (or sync) filled.

They read the lake only: no token and no network (``fake_api`` has no routes, so a
request would fail the test).
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.core.store import LakeStore, params_hash

NOW = datetime.now(timezone.utc)
EXPAND = "reports,users"
PARAMS_ALL = {"$expand": EXPAND, "$top": "1000"}
PARAMS_TOP50 = {"$top": "50"}
PARAMS_ALICE = {"userId": "alice@example.com"}

OLD_GROUPS = {"value": [{"id": "old", "name": "Old workspace"}]}
NEW_GROUPS = {"value": [{"id": "new", "name": "New workspace"}, {"id": "n2"}]}
TOP50_GROUPS = {"value": [{"id": "t50", "name": "Top fifty"}]}
ACCESS = {"artifacts": [{"ArtifactId": "a1"}]}


def run(*args: str, input: Optional[str] = None):
    return CliRunner().invoke(app, list(args), input=input)


def put(
    store: LakeStore,
    endpoint: str,
    params: Dict[str, str],
    data: Any,
    *,
    tenant: str = "tenant-1",
    age: timedelta = timedelta(0),
    rows: Optional[int] = None,
):
    return store.write_snapshot(
        tenant,
        endpoint,
        params,
        data,
        rows=rows,
        fetched_at=NOW - age,
        request={"method": "GET", "path": "/x", "params": params},
    )


@pytest.fixture
def store(cache_folder, fake_api) -> LakeStore:
    return LakeStore(cache_folder / "lake")


@pytest.fixture
def seeded(store) -> LakeStore:
    """Two requests of ``admin.groups`` (one with two versions), one of a user, events."""
    put(store, "admin.groups", PARAMS_ALL, OLD_GROUPS, age=timedelta(days=3), rows=1)
    put(store, "admin.groups", PARAMS_ALL, NEW_GROUPS, age=timedelta(minutes=5), rows=2)
    put(
        store,
        "admin.groups",
        PARAMS_TOP50,
        TOP50_GROUPS,
        age=timedelta(hours=2),
        rows=1,
    )
    put(store, "admin.users.artifact_access", PARAMS_ALICE, ACCESS, rows=1)
    store.append_events(
        "tenant-1",
        "admin.activityevents",
        date(2026, 9, 28),
        [
            {"Id": "e1", "Activity": "ViewReport"},
            {"Id": "e2", "Activity": "EditReport"},
        ],
        sealed=True,
    )
    store.append_events(
        "tenant-1",
        "admin.activityevents",
        date(2026, 9, 29),
        [{"Id": "e3", "Activity": "ViewReport"}],
    )
    return store


# ---------------------------------------------------------------------------
# no lake, empty lake
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [["ls"], ["show", "admin.groups"], ["prune", "--yes"]],
    ids=["ls", "show", "prune"],
)
def test_without_a_cache_folder_the_commands_say_what_to_do(fake_api, args):
    result = run("lake", *args)

    assert result.exit_code == 1
    assert "There is no data lake yet." in result.output
    assert "pbi config set-cache-folder" in result.output


def test_lake_group_without_a_command_shows_a_hint():
    result = run("lake")

    assert result.exit_code == 0
    assert "Use pbi lake --help" in result.output


def test_ls_of_an_empty_lake(cache_folder, store):
    result = run("lake", "ls")

    assert result.exit_code == 0
    assert f"Data lake: {cache_folder / 'lake'}" in result.stdout
    assert "The lake is empty" in result.stdout


def test_show_of_an_empty_lake_says_so(store):
    result = run("lake", "show", "admin.groups")

    assert result.exit_code == 1
    assert "The lake is empty" in result.output


def test_the_lake_commands_work_while_caching_is_disabled(seeded):
    """Switching caching off stops the commands from using the lake, not from browsing it."""
    from pbi_cli.config import PBIConfig

    PBIConfig().cache_enabled = False

    assert run("lake", "ls").exit_code == 0
    assert run("lake", "show", "admin.users.artifact_access").exit_code == 0


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def table(output: str) -> List[Dict[str, str]]:
    """The rows of the table ``pbi lake ls`` prints, as ``{column: cell}``.

    Columns are separated by two spaces or more; no cell is empty (``-`` is used).
    """
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("ENDPOINT"))
    header = re.split(r"\s{2,}", lines[start].strip())
    rows = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        cells = re.split(r"\s{2,}", line.strip())
        assert len(cells) == len(header), line
        rows.append(dict(zip(header, cells)))
    return rows


def test_ls_lists_one_line_per_request(seeded, fake_api):
    result = run("lake", "ls")

    assert result.exit_code == 0
    assert "Tenant: tenant-1" in result.stdout
    rows = {row["REF"]: row for row in table(result.stdout)}
    assert sorted(rows) == sorted(
        [
            params_hash(PARAMS_ALL),
            params_hash(PARAMS_TOP50),
            params_hash(PARAMS_ALICE),
            "events",
        ]
    )

    newest = rows[params_hash(PARAMS_ALL)]  # two versions: the newest is shown
    assert newest["ENDPOINT"] == "admin.groups"
    assert newest["AGE"] == "5 min"
    assert newest["ROWS"] == "2"
    assert newest["VERSIONS"] == "2"
    assert newest["SIZE"].endswith(" B")
    assert newest["PARAMETERS"] == f"$expand={EXPAND} $top=1000"

    top50 = rows[params_hash(PARAMS_TOP50)]
    assert (top50["AGE"], top50["ROWS"], top50["VERSIONS"]) == ("2 h", "1", "1")
    assert top50["PARAMETERS"] == "$top=50"

    alice = rows[params_hash(PARAMS_ALICE)]
    assert alice["ENDPOINT"] == "admin.users.artifact_access"
    assert alice["PARAMETERS"] == "userId=alice@example.com"
    assert fake_api.calls() == []


def test_ls_shows_the_columns(seeded):
    out = run("lake", "ls").stdout
    (header,) = [line for line in out.splitlines() if line.startswith("ENDPOINT")]

    assert re.split(r"\s{2,}", header) == [
        "ENDPOINT",
        "REF",
        "FETCHED (UTC)",
        "AGE",
        "ROWS",
        "SIZE",
        "VERSIONS",
        "PARAMETERS",
    ]


def test_ls_filters_by_endpoint(seeded):
    out = run("lake", "ls", "-e", "admin.groups").stdout

    assert "admin.groups" in out
    assert "admin.users.artifact_access" not in out
    assert "admin.activityevents" not in out


def test_ls_accepts_the_folder_spelling_of_an_endpoint(seeded):
    out = run("lake", "ls", "-e", "admin_groups").stdout

    assert "admin.groups" in out


def test_ls_with_all_versions_lists_every_version(seeded):
    out = run("lake", "ls", "-e", "admin.groups", "--all-versions").stdout
    rows = table(out)

    assert len(rows) == 3
    assert list(rows[0]) == [
        "ENDPOINT",
        "REF",
        "VERSION",
        "FETCHED (UTC)",
        "AGE",
        "ROWS",
        "SIZE",
        "PARAMETERS",
    ]
    all_versions = [r for r in rows if r["REF"] == params_hash(PARAMS_ALL)]
    assert sorted(r["AGE"] for r in all_versions) == ["3 d", "5 min"]
    versions = {r["VERSION"] for r in rows}
    assert len(versions) == 3
    assert all(re.fullmatch(r"\d{8}T\d{12}Z", v) for v in versions)


def test_ls_shows_event_logs_by_day(seeded):
    (row,) = [r for r in table(run("lake", "ls").stdout) if r["REF"] == "events"]

    assert row["ENDPOINT"] == "admin.activityevents"
    assert row["ROWS"] == "3"  # 2 + 1 events
    assert row["VERSIONS"] == "2 day(s)"
    assert row["PARAMETERS"] == "newest day 2026-09-29 (open)"
    assert row["AGE"] == "0 s"  # written a moment ago

    per_day = table(
        run("lake", "ls", "-e", "admin.activityevents", "--all-versions").stdout
    )
    assert [(r["VERSION"], r["ROWS"], r["PARAMETERS"]) for r in per_day] == [
        ("2026-09-29", "1", "open"),
        ("2026-09-28", "2", "sealed"),
    ]


def test_ls_nothing_matches(seeded):
    result = run("lake", "ls", "-e", "admin.dataflows")

    assert result.exit_code == 0
    assert "Nothing matches" in result.output


def test_ls_groups_by_tenant_and_filters_by_tenant(seeded):
    put(seeded, "admin.groups", PARAMS_TOP50, TOP50_GROUPS, tenant="tenant-2")

    both = run("lake", "ls", "-e", "admin.groups").stdout
    only = run("lake", "ls", "-t", "tenant-2").stdout

    assert "Tenant: tenant-1" in both and "Tenant: tenant-2" in both
    assert "Tenant: tenant-1" not in only and "Tenant: tenant-2" in only


def test_ls_unknown_tenant_lists_the_known_ones(seeded):
    result = run("lake", "ls", "-t", "nobody")

    assert result.exit_code == 1
    assert "no data of tenant 'nobody'" in result.output
    assert "tenant-1" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_the_stored_response(seeded):
    result = run("lake", "show", "admin.users.artifact_access")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == ACCESS


def test_show_tells_requests_apart_when_there_are_several(seeded):
    result = run("lake", "show", "admin.groups")

    assert result.exit_code == 1
    assert "2 stored requests of admin.groups match" in result.output
    assert params_hash(PARAMS_ALL) in result.output
    assert params_hash(PARAMS_TOP50) in result.output
    assert "$top=50" in result.output


def test_show_picks_a_request_by_parameter(seeded):
    result = run("lake", "show", "admin.groups", "-p", "$top=50")

    assert json.loads(result.stdout) == TOP50_GROUPS


def test_show_shows_the_newest_version_by_default(seeded):
    result = run("lake", "show", "admin.groups", "-p", "$top=1000")

    assert json.loads(result.stdout) == NEW_GROUPS


def test_show_compares_lists_of_values_unordered(seeded):
    """``$expand=users,reports`` is the request stored as ``reports,users``."""
    result = run("lake", "show", "admin.groups", "-p", "$expand=users,reports")

    assert json.loads(result.stdout) == NEW_GROUPS


def test_show_needs_every_given_parameter_to_match(seeded):
    result = run("lake", "show", "admin.groups", "-p", "$top=50", "-p", "$expand=users")

    assert result.exit_code == 1
    assert "No stored request of admin.groups matches" in result.output


def test_show_picks_a_request_by_ref(seeded):
    ref = params_hash(PARAMS_TOP50)

    by_ref = run("lake", "show", "admin.groups", "--ref", ref[:6])

    assert json.loads(by_ref.stdout) == TOP50_GROUPS


def test_show_picks_an_older_version(seeded):
    versions = seeded.versions("tenant-1", "admin.groups", PARAMS_ALL)
    oldest = versions[-1].version

    exact = run("lake", "show", "admin.groups", "-p", "$top=1000", "-v", oldest)
    prefix = run("lake", "show", "admin.groups", "-p", "$top=1000", "-v", oldest[:12])

    assert json.loads(exact.stdout) == OLD_GROUPS
    assert json.loads(prefix.stdout) == OLD_GROUPS


def test_show_unknown_version(seeded):
    result = run("lake", "show", "admin.groups", "-p", "$top=50", "-v", "1999")

    assert result.exit_code == 1
    assert "No version '1999' of this request" in result.output


def test_show_manifest(seeded):
    result = run("lake", "show", "admin.groups", "-p", "$top=50", "--manifest")

    manifest = json.loads(result.stdout)
    assert manifest["endpoint"] == "admin.groups"
    assert manifest["params"] == PARAMS_TOP50
    assert manifest["tenant"] == "tenant-1"
    assert len(manifest["sha256"]) == 64
    assert "authorization" not in result.stdout.lower()


def test_show_unknown_endpoint_lists_what_is_there(seeded):
    result = run("lake", "show", "admin.dataflows")

    assert result.exit_code == 1
    assert "holds nothing for 'admin.dataflows'" in result.output
    assert "admin.groups" in result.output


def test_show_rejects_a_parameter_without_a_value(seeded):
    result = run("lake", "show", "admin.groups", "-p", "top")

    assert result.exit_code == 2
    assert "not KEY=VALUE" in result.output


def test_show_needs_a_tenant_when_the_lake_holds_several(seeded):
    put(
        seeded,
        "admin.users.artifact_access",
        PARAMS_ALICE,
        {"artifacts": []},
        tenant="tenant-2",
    )

    ambiguous = run("lake", "show", "admin.users.artifact_access")
    chosen = run("lake", "show", "admin.users.artifact_access", "-t", "tenant-2")

    assert ambiguous.exit_code == 1
    assert "several tenants (tenant-1, tenant-2)" in ambiguous.output
    assert json.loads(chosen.stdout) == {"artifacts": []}


def test_show_an_event_log_needs_a_day(seeded):
    result = run("lake", "show", "admin.activityevents")

    assert result.exit_code == 1
    assert "--day YYYY-MM-DD" in result.output
    assert "2026-09-29, 2026-09-28" in result.output


def test_show_events_prints_one_event_per_line(seeded):
    result = run("lake", "show", "admin.activityevents", "--day", "2026-09-28")

    assert result.exit_code == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["Id"] for e in events] == ["e1", "e2"]


def test_show_events_manifest(seeded):
    result = run("lake", "show", "admin.activityevents", "-d", "2026-09-28", "-m")

    manifest = json.loads(result.stdout)
    assert manifest["rows"] == 2 and manifest["sealed"] is True


def test_show_events_of_a_day_without_events(seeded):
    result = run("lake", "show", "admin.activityevents", "--day", "2026-01-01")

    assert result.exit_code == 1
    assert "No events of 2026-01-01" in result.output


def test_show_events_rejects_a_bad_date(seeded):
    result = run("lake", "show", "admin.activityevents", "--day", "yesterday")

    assert result.exit_code == 2
    assert "not a date" in result.output


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def versions(store: LakeStore, endpoint: str, params: Dict[str, str]) -> int:
    return len(store.versions("tenant-1", endpoint, params))


def test_prune_asks_before_deleting(seeded):
    result = run("lake", "prune", input="n\n")

    assert result.exit_code == 1
    assert "Delete the older versions" in result.output
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 2


def test_prune_keeps_the_newest_version_of_each_request(seeded):
    result = run("lake", "prune", "--yes")

    assert result.exit_code == 0
    assert (
        "Deleted 1 old version(s), kept the newest 1 of each request" in result.output
    )
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 1
    newest = seeded.latest("tenant-1", "admin.groups", PARAMS_ALL)
    assert newest is not None and newest.load() == NEW_GROUPS


def test_prune_after_answering_yes(seeded):
    result = run("lake", "prune", input="y\n")

    assert result.exit_code == 0
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 1


def test_prune_keeps_as_many_as_asked(seeded):
    result = run("lake", "prune", "--keep", "2", "--yes")

    assert "Deleted 0 old version(s)" in result.output
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 2


def test_prune_can_be_limited_to_one_endpoint(seeded):
    put(
        seeded,
        "admin.users.artifact_access",
        PARAMS_ALICE,
        ACCESS,
        age=timedelta(days=1),
    )

    run("lake", "prune", "-e", "admin.users.artifact_access", "--yes")

    assert versions(seeded, "admin.users.artifact_access", PARAMS_ALICE) == 1
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 2


def test_prune_can_be_limited_to_one_tenant(seeded):
    put(
        seeded,
        "admin.groups",
        PARAMS_ALL,
        OLD_GROUPS,
        tenant="tenant-2",
        age=timedelta(days=2),
    )
    put(seeded, "admin.groups", PARAMS_ALL, NEW_GROUPS, tenant="tenant-2")

    run("lake", "prune", "-t", "tenant-2", "--yes")

    assert len(seeded.versions("tenant-2", "admin.groups", PARAMS_ALL)) == 1
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 2


def test_prune_leaves_event_logs_alone(seeded):
    run("lake", "prune", "--yes")

    assert len(seeded.event_days("tenant-1", "admin.activityevents")) == 2


def test_prune_rejects_keeping_nothing(seeded):
    result = run("lake", "prune", "--keep", "0", "--yes")

    assert result.exit_code == 2
    assert versions(seeded, "admin.groups", PARAMS_ALL) == 2


@pytest.mark.parametrize(
    "delta, text",
    [
        (timedelta(seconds=-5), "0 s"),
        (timedelta(seconds=0), "0 s"),
        (timedelta(seconds=89), "89 s"),
        (timedelta(seconds=90), "2 min"),
        (timedelta(minutes=59), "59 min"),
        (timedelta(minutes=60), "1 h"),
        (timedelta(hours=5, minutes=20), "5 h"),
        (timedelta(hours=47), "47 h"),
        (timedelta(hours=48), "2 d"),
        (timedelta(days=30), "30 d"),
    ],
)
def test_ages_are_short(delta, text):
    from pbi_cli.cli_support import format_age

    assert format_age(delta) == text
