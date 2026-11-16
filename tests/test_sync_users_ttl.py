"""The users of items asked for one item at a time stay fresh for a week in a sync, so that a
big tenant does not spend its whole quota asking for them again every day."""

from datetime import timedelta

import pytest
from sync_helpers import World

from pbi_cli.core.sync.targets import USERS_TTL, get_target


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def users_calls(world):
    return len(world.fake.calls_to(r"/reports/[^/]+/users$"))


def test_the_users_of_the_items_of_an_administrator_stay_fresh_for_a_week():
    for name in (
        "group-users",
        "report-users",
        "dataset-users",
        "dashboard-users",
        "dataflow-users",
        "app-users",
    ):
        assert get_target(name).ttl == USERS_TTL == timedelta(days=7)
    assert get_target("reports").ttl is None  # the lists keep the endpoint's day


def test_a_sync_days_later_does_not_ask_for_the_users_again(world):
    world.run("report-users")
    asked = users_calls(world)
    assert asked

    world.clock.advance(days=3)
    world.run("report-users")

    # the reports are asked for again, not their users
    assert users_calls(world) == asked
    assert len(world.fake.calls_to(r"/admin/reports$")) == 2


def test_after_a_week_they_are_asked_for_again(world):
    world.run("report-users")
    asked = users_calls(world)

    world.clock.advance(days=7, hours=1)
    world.run("report-users")

    assert users_calls(world) == 2 * asked


def test_max_age_overrules_the_week(world):
    world.run("report-users")
    asked = users_calls(world)

    world.clock.advance(days=2)
    world.run("report-users", max_age=timedelta(days=1))

    assert users_calls(world) == 2 * asked
