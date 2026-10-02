"""The catalog of things a sync can fetch."""

import re

import pytest

from pbi_cli.core.registry import Kind, Scope, get_endpoint
from pbi_cli.core.sync.targets import (
    ALL,
    DEFAULT,
    TARGETS,
    Mode,
    children_of,
    get_target,
    select_targets,
)
from pbi_cli.errors import PBIError

PLAIN = [
    "groups",
    "apps",
    "capacities",
    "reports",
    "datasets",
    "dashboards",
    "dataflows",
]


# -- the catalog is consistent with the registry -------------------------------------


@pytest.mark.parametrize("target", TARGETS, ids=lambda t: t.name)
def test_every_target_uses_an_operation_of_the_registry(target):
    endpoint = get_endpoint(target.endpoint)

    assert endpoint.scope is target.scope
    if target.mode in (Mode.SNAPSHOT, Mode.FANOUT):
        assert endpoint.kind is Kind.SNAPSHOT
    if target.mode is Mode.EVENTS:
        assert endpoint.kind is Kind.EVENTS
    if target.mode is Mode.SCAN:
        assert endpoint.kind is Kind.JOB


@pytest.mark.parametrize(
    "target", [t for t in TARGETS if t.mode is Mode.FANOUT], ids=lambda t: t.name
)
def test_a_fan_out_fills_every_placeholder_of_its_path(target):
    endpoint = get_endpoint(target.endpoint)

    assert set(target.bind) == set(endpoint.path_params)
    assert all(source in ("row", "param") for source, _ in target.bind.values())
    parent = get_target(target.parent)
    assert parent.scope is target.scope  # one token serves both
    for source, name in target.bind.values():
        if source == "param":  # a parameter of the parent's own request
            assert name in get_endpoint(parent.endpoint).path_params


def test_parents_come_before_their_children():
    names = [t.name for t in TARGETS]

    for target in TARGETS:
        if target.parent:
            assert names.index(target.parent) < names.index(target.name)


def test_target_names_are_unique_and_not_reserved():
    names = [t.name for t in TARGETS]

    assert len(names) == len(set(names))
    assert DEFAULT not in names and ALL not in names


def test_what_is_not_plain_says_why():
    for target in TARGETS:
        if target.default:
            assert target.sensitive == ""
            assert target.scope is Scope.ADMIN and target.mode is Mode.SNAPSHOT
        elif target.scope is Scope.ADMIN:
            assert target.sensitive, f"{target.name} is opt-in and should say why"


def test_children_of():
    assert [t.name for t in children_of("reports")] == ["report-users"]
    assert [t.name for t in children_of("user-reports")] == ["user-pages"]
    assert children_of("groups") == []


def test_get_target_says_what_there_is():
    with pytest.raises(PBIError, match="Unknown sync target 'nope'") as info:
        get_target("nope")

    assert "groups" in str(info.value) and "'default' and 'all'" in str(info.value)


# -- selecting -------------------------------------------------------------------------


def test_without_names_the_plain_targets_are_synced():
    selection = select_targets()

    assert selection.names == PLAIN
    assert selection.implied == frozenset()
    assert select_targets([DEFAULT]).names == PLAIN


def test_naming_targets_selects_only_those():
    assert select_targets(["scan"]).names == ["scan"]
    assert select_targets(["groups", "apps"]).names == ["groups", "apps"]


def test_the_order_of_the_catalog_wins_over_the_order_given():
    assert select_targets(["apps", "groups"]).names == ["groups", "apps"]


def test_default_and_more():
    assert select_targets([DEFAULT, "activity"]).names == PLAIN + ["activity"]


def test_all_is_everything():
    assert select_targets([ALL]).names == [t.name for t in TARGETS]


def test_a_fan_out_brings_the_target_it_needs():
    selection = select_targets(["report-users"])

    assert selection.names == ["reports", "report-users"]
    assert selection.implied == {"reports"}


def test_a_chain_of_fan_outs_brings_every_link():
    selection = select_targets(["user-pages"])

    assert selection.names == ["user-groups", "user-reports", "user-pages"]
    assert selection.implied == {"user-groups", "user-reports"}


def test_a_target_that_is_named_is_not_implied():
    selection = select_targets(["reports", "report-users"])

    assert selection.names == ["reports", "report-users"]
    assert selection.implied == frozenset()


def test_repeating_a_name_changes_nothing():
    assert select_targets(["groups", "groups", DEFAULT]).names == PLAIN


def test_an_unknown_name_is_refused():
    with pytest.raises(PBIError, match="Unknown sync target 'repots'"):
        select_targets(["groups", "repots"])


def test_no_target_name_looks_like_another_spelling_of_a_path():
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)*", t.name) for t in TARGETS)


# -- what the accounts that are stored can do ----------------------------------------

SKELETON = [
    "user-groups",
    "user-apps",
    "user-reports",
    "user-datasets",
    "user-dashboards",
    "user-dataflows",
]
BOTH = {Scope.ADMIN, Scope.USER}
ONLY_ADMIN = {Scope.ADMIN}
ONLY_USER = {Scope.USER}


def test_every_account_may_be_assumed_when_nothing_is_said_about_them():
    assert select_targets().names == select_targets(available=None).names == PLAIN


def test_the_plain_sync_is_the_administrators_lists_when_there_is_an_administrator():
    assert select_targets(available=BOTH).names == PLAIN
    assert select_targets(available=ONLY_ADMIN).names == PLAIN


def test_the_plain_sync_of_a_user_is_what_a_user_can_see_workspace_by_workspace():
    selection = select_targets(available=ONLY_USER)

    assert selection.names == SKELETON
    assert all(t.scope is Scope.USER for t in selection.targets)


def test_without_any_account_there_is_no_plain_sync():
    assert select_targets(available=set()).names == []


def test_only_the_skeleton_of_a_user_is_plain_for_a_user():
    assert [t.name for t in TARGETS if t.default_user] == SKELETON
    assert not any(t.default and t.default_user for t in TARGETS)


def test_a_target_that_needs_the_missing_account_is_refused_and_says_what_to_do():
    with pytest.raises(PBIError) as refused:
        select_targets(["scan"], available=ONLY_USER)

    message = str(refused.value)
    assert "'scan' needs an administrator account" in message
    assert "`pbi auth -t <token> -g admin`" in message
    assert "The accounts you have are: user." in message


def test_a_user_target_without_a_user_account_is_refused():
    with pytest.raises(PBIError) as refused:
        select_targets(["user-pages"], available=ONLY_ADMIN)

    assert "'user-pages' needs a user account" in str(refused.value)
    assert "-g user" in str(refused.value)


def test_with_no_account_the_message_does_not_list_any():
    with pytest.raises(PBIError) as refused:
        select_targets(["groups"], available=set())

    assert "none is stored" in str(refused.value)
    assert "accounts you have" not in str(refused.value)


def test_all_is_what_the_accounts_can_run():
    admin = select_targets([ALL], available=ONLY_ADMIN)
    user = select_targets([ALL], available=ONLY_USER)

    assert all(t.scope is Scope.ADMIN for t in admin.targets) and admin.names
    assert user.names == [t.name for t in TARGETS if t.scope is Scope.USER]
    assert select_targets([ALL], available=BOTH).names == [t.name for t in TARGETS]


def test_a_fan_out_of_a_user_still_brings_its_parents_when_only_a_user_is_stored():
    selection = select_targets(["user-pages"], available=ONLY_USER)

    assert selection.names == ["user-groups", "user-reports", "user-pages"]
    assert selection.implied == {"user-groups", "user-reports"}
