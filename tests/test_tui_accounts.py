"""The TUI with several accounts, or with only a user account."""

from datetime import timedelta

import pytest
from core_helpers import make_client, make_token
from sync_helpers import TENANT, World
from textual.widgets import DataTable, Input, SelectionList
from tui_helpers import backend_of, run_ui

from pbi_cli.core.registry import Scope
from pbi_cli.errors import AuthError
from pbi_cli.tui.backend import AccountInfo
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import AccountsModal, SignInModal, format_left

SKELETON = [
    "user-groups",
    "user-apps",
    "user-reports",
    "user-datasets",
    "user-dashboards",
    "user-dataflows",
]


def messages(ui):
    return [note.message for note in ui.app._notifications]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def user_world(world):
    """Only a user is signed in, and what a user can see has been synced."""
    world.only_user()
    world.run()
    return world


def user_backend(world, **replace):
    return backend_of(world, client_for=world.engine._client_for, **replace)


# ---------------------------------------------------------------------------
# the header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width, lines", [(200, 1), (100, 2), (70, 3)])
def test_the_header_wraps_when_it_is_long_so_that_the_lake_is_never_cut_off(
    world, width, lines
):
    world.accounts(ana="oid-ana", bob="oid-bob")

    async def scenario(ui):
        bar = ui.app.screen.query_one("StatusBar")
        who = ui.app.screen.query_one("#who")
        return bar.size.height, who.size.height, who.visual.plain

    bar, who, text = run_ui(user_backend(world), scenario, size=(width, 30))

    assert bar == who == lines  # it grows with what it has to say
    assert text.endswith(
        "lake"
    )  # and the lake is the last thing it says: all of it is shown


def test_the_header_shows_every_account_that_is_signed_in(world):
    world.run("groups")

    async def scenario(ui):
        return ui.static("#who")

    who = run_ui(backend_of(world), scenario)

    assert "admin-nlm (admin)" in who and "user-nlm (user)" in who
    assert who.count("token") == 2


def test_the_header_of_someone_with_only_a_user_account_has_that_account(user_world):
    async def scenario(ui):
        return ui.static("#who")

    who = run_ui(user_backend(user_world), scenario)

    assert "user-nlm (user)" in who and "(admin)" not in who and who.count("token") == 1


def test_the_header_without_any_account_says_so(world):
    world.run("groups")

    def nobody(scope, profile=None):
        raise AuthError("no profile")

    async def scenario(ui):
        return ui.static("#who")

    who = run_ui(backend_of(world, client_for=nobody), scenario)

    assert "no profile" in who and "not signed in" in who


# ---------------------------------------------------------------------------
# the Explorer of a user
# ---------------------------------------------------------------------------


def test_the_explorer_shows_what_a_user_can_see(user_world):
    async def scenario(ui):
        tree = ui.tree()
        await ui.select("workspace", "ws-0001")
        return tree, ui.rows(), ui.static("#info")

    tree, rows, info = run_ui(user_backend(user_world), scenario)

    assert tree[0] == "● Workspaces  3"
    assert [line for line in tree if "Apps" in line] == ["● Apps  3"]
    assert [(r[0], r[1]) for r in rows] == [
        ("Report 1", "Report"),
        ("Dataset 1", "Dataset"),
        ("Dashboard 1", "Dashboard"),
        ("Dataflow 1", "Dataflow"),
        ("App 1", "App"),
    ]
    assert "Visible to" in info and "user-nlm" in info
    assert "user.groups" in info and "admin" not in info.lower().replace(
        "administrator", ""
    )


def test_a_workspace_with_the_lists_of_a_user_does_not_say_they_are_missing(
    user_world,
):
    async def scenario(ui):
        await ui.select("workspace", "ws-0002")  # it holds fewer kinds than ws-0001
        return ui.static("#info")

    info = run_ui(user_backend(user_world), scenario)

    assert "not in the lake" not in info


def test_a_workspace_without_the_lists_of_a_user_says_they_are_missing(world):
    world.only_user()
    world.run("user-groups")  # the workspaces, and nothing in them

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        return ui.static("#info")

    info = run_ui(user_backend(world), scenario)

    assert "the list is not in the lake" in info


def test_an_item_that_only_a_user_listed_names_that_list_as_its_source(user_world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        return ui.static("#info")

    info = run_ui(user_backend(user_world), scenario)

    assert "user.group_reports of the workspace, fetched" in info


def test_r_of_a_user_fetches_what_the_user_can_see_not_a_scan(user_world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        return ui.explorer.refresh_choice()

    title, options = run_ui(user_backend(user_world), scenario)

    assert title == "Fetch what you can see"
    assert options.targets == ("default",) and options.force is True


def test_r_of_a_user_on_the_apps_fetches_the_apps_of_the_account(user_world):
    async def scenario(ui):
        await ui.select("apps")
        return ui.explorer.refresh_choice()

    title, options = run_ui(user_backend(user_world), scenario)

    assert title == "Fetch your apps"
    assert options.targets == ("user-apps",) and options.force is True


def test_r_of_an_administrator_still_scans_the_workspace(world):
    world.run("groups")

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        return ui.explorer.refresh_choice()

    title, options = run_ui(backend_of(world), scenario)

    assert title.startswith("Scan ") and options.targets == ("scan",)


def test_a_user_is_asked_before_the_lists_they_can_see_are_fetched_again(user_world):
    async def scenario(ui):
        before = type(ui.app.screen).__name__
        await ui.press("r")  # the Lake node is selected at first
        return before, type(ui.app.screen).__name__

    screens = run_ui(user_backend(user_world), scenario)

    assert screens == ("ExplorerScreen", "ConfirmModal")


def test_the_activity_of_a_tenant_is_not_offered_to_a_user(user_world):
    user_world.run("user-groups")  # nothing of activity is in this lake
    backend = user_backend(user_world)

    async def scenario(ui):
        ui.explorer._ref = type(ui.explorer._ref)("activity", "")
        choice = ui.explorer.refresh_choice()
        ui.explorer.action_refresh()
        await ui.settle()
        return choice, messages(ui)

    choice, notes = run_ui(backend, scenario)

    assert choice is None
    assert any("Only an administrator account" in note for note in notes)


# ---------------------------------------------------------------------------
# the Sync screen
# ---------------------------------------------------------------------------


def test_a_user_without_an_administrator_has_the_users_plain_targets_chosen(user_world):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        picked = list(ui.sync.query_one("#targets", SelectionList).selected)
        options = ui.sync.query_one("#targets", SelectionList)
        return (
            picked,
            options.get_option("groups").disabled,
            options.get_option("user-groups").disabled,
        )

    picked, admin_disabled, user_disabled = run_ui(user_backend(user_world), scenario)

    assert picked == SKELETON
    assert admin_disabled is True and user_disabled is False


def test_the_dimmed_targets_say_why_and_what_to_do(user_world):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        await ui.settle()
        return ui.static("#targets-note", ui.sync)

    note = run_ui(user_backend(user_world), scenario)

    assert "user-groups" in note  # the target that is highlighted
    assert (
        "The dimmed targets need an administrator account, and none is stored" in note
    )
    assert "pbi auth -t <token> -g admin" in note and "press p" in note


def test_nothing_is_said_about_a_missing_account_when_both_are_stored(world):
    world.run("groups")

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        await ui.settle()
        return ui.static("#targets-note", ui.sync)

    note = run_ui(backend_of(world), scenario)

    assert "groups: Workspaces" in note and "dimmed" not in note


def test_a_user_can_run_what_the_user_can_see_and_the_explorer_shows_it(world):
    world.only_user()
    world.run("user-groups")  # the workspaces only: nothing in them yet

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        before = ui.rows()
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        await ui.pilot.click("#run")
        await ui.until(
            lambda: ui.app.run_state is not None and not ui.app.run_state.running
        )
        await ui.finish_sync()
        await ui.press("escape")
        await ui.select("workspace", "ws-0001")
        return before, ui.rows(), ui.app.run_state.options.targets

    before, after, ran = run_ui(user_backend(world), scenario)

    assert before == [] and len(after) == 5
    assert set(ran) == set(SKELETON)
    assert not world.fake.calls_to(r"^/admin")


def test_the_plan_says_which_accounts_it_uses(world):
    world.run("groups")

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        return ui.static("#plan-head", ui.sync)

    head = run_ui(backend_of(world), scenario)

    assert "Tenant: tenant-1" in head and "Accounts: admin-nlm (admin)" in head
    assert "user-nlm" not in head  # the plain sync of an administrator needs no user


def test_the_plan_of_someone_with_only_a_user_account_names_that_account(user_world):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        return ui.static("#plan-head", ui.sync)

    head = run_ui(user_backend(user_world), scenario)

    assert "Accounts: user-nlm (user)" in head and "admin-nlm" not in head


def test_the_targets_are_dimmed_again_when_the_accounts_change(world):
    world.run("groups")
    signed = {"admin": True}

    def client_for(scope, profile=None):
        if scope is Scope.ADMIN and not signed["admin"]:
            raise AuthError("No active profile set for group 'admin'.")
        return world.client_for(scope)

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        options = ui.sync.query_one("#targets", SelectionList)
        before = options.get_option("groups").disabled
        said_before = ui.static("#targets-note", ui.sync)
        signed["admin"] = False
        await ui.press("escape")
        await ui.press("s")
        await ui.settle()
        return (
            before,
            options.get_option("groups").disabled,
            "groups" in options.selected,
            said_before,
            ui.static("#targets-note", ui.sync),
        )

    before, after, still_chosen, said_before, said_after = run_ui(
        backend_of(world, client_for=client_for), scenario
    )

    assert before is False and after is True and still_chosen is False
    assert "dimmed" not in said_before and "The dimmed targets need" in said_after


def test_a_target_is_enabled_again_when_its_account_is_stored(world):
    world.run("groups")
    signed = {"admin": False}

    def client_for(scope, profile=None):
        if scope is Scope.ADMIN and not signed["admin"]:
            raise AuthError("No active profile set for group 'admin'.")
        return world.client_for(scope)

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        options = ui.sync.query_one("#targets", SelectionList)
        before = options.get_option("groups").disabled
        signed["admin"] = True
        await ui.press("escape")
        await ui.press("s")
        await ui.settle()
        return before, options.get_option("groups").disabled

    before, after = run_ui(backend_of(world, client_for=client_for), scenario)

    assert before is True and after is False


# ---------------------------------------------------------------------------
# signing in names the account
# ---------------------------------------------------------------------------


def test_the_dialog_asks_for_the_kind_of_token_that_is_missing(world):
    async def scenario(ui):
        ui.app.explain_sync_problem(
            AuthError("The token for profile 'svc' expired.", group="user"), None
        )
        await ui.settle()
        modal = ui.app.screen
        return (
            type(modal).__name__,
            modal._selected_group(),
            modal.query_one("#profile", Input).value,
        )

    name, group, profile = run_ui(backend_of(world), scenario)

    assert name == "SignInModal" and group == "user" and profile == "user-nlm"


def test_an_error_that_does_not_say_which_kind_asks_for_the_administrators(world):
    async def scenario(ui):
        ui.app.explain_sync_problem(AuthError("no token"), None)
        await ui.settle()
        return ui.app.screen._selected_group()

    assert run_ui(backend_of(world), scenario) == "admin"


# ---------------------------------------------------------------------------
# the Accounts dialog
# ---------------------------------------------------------------------------

NOW_PLUS = timedelta(minutes=40)


def accounts_of(world):
    soon = world.clock.now() + NOW_PLUS
    return [
        AccountInfo("admin", "adm", True, "adm@x.com", "tenant-1", soon),
        AccountInfo("user", "svc-finance", True, "svc@x.com", "tenant-1", soon),
        AccountInfo(
            "user", "ana", False, "ana@x.com", "tenant-1", world.clock.now() - NOW_PLUS
        ),
        AccountInfo("user", "old", False, None, None, None, has_token=False),
    ]


@pytest.mark.parametrize(
    "seconds, text",
    [
        (30, "1 min"),
        (40 * 60, "40 min"),
        (89 * 60, "89 min"),
        (90 * 60, "1 h"),
        (5 * 3600, "5 h"),
        (47 * 3600, "47 h"),
        (48 * 3600, "2 d"),
        (10 * 86400, "10 d"),
    ],
)
def test_how_long_a_token_lasts_is_told_in_the_largest_unit_that_keeps_it_short(
    seconds, text
):
    assert format_left(seconds) == text


def test_the_dialog_lists_every_profile_with_who_and_how_long_it_lasts(world):
    world.run("groups")
    backend = backend_of(world, accounts=lambda: accounts_of(world))

    async def scenario(ui):
        await ui.press("p")
        assert isinstance(ui.app.screen, AccountsModal)
        return ui.rows("#accounts")

    rows = run_ui(backend, scenario)

    assert [r[:4] for r in rows] == [
        ["admin", "adm", "yes", "adm@x.com"],
        ["user", "svc-finance", "yes", "svc@x.com"],
        ["user", "ana", "", "ana@x.com"],
        ["user", "old", "", "-"],
    ]
    assert [r[5] for r in rows] == [
        "40 min left",
        "40 min left",
        "expired",
        "none stored",
    ]


def test_enter_makes_the_profile_the_active_one_of_its_group(world):
    world.run("groups")
    activated = []
    backend = backend_of(
        world,
        accounts=lambda: accounts_of(world),
        activate=lambda group, profile: activated.append((group, profile)),
    )

    async def scenario(ui):
        await ui.press("p")
        ui.app.screen.query_one("#accounts", DataTable).move_cursor(row=2)
        await ui.press("enter")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert activated == [("user", "ana")] and screen == "ExplorerScreen"
    assert any("ana is now the active profile of the group user" in n for n in notes)


def test_the_header_follows_the_profile_that_is_made_active(world):
    world.run("groups")
    active = {"user": "svc-finance"}
    clients = {}

    def client_for(scope, profile=None):
        if scope is Scope.ADMIN:
            return world.admin
        name = profile or active["user"]
        if name not in clients:
            token = make_token(
                tenant=TENANT, expires_in=timedelta(days=3650), oid=f"oid-{name}"
            )
            clients[name] = make_client(
                world.fake,
                clock=world.clock,
                store=world.store,
                token=token,
                group="user",
                profile=name,
            )[0]
        return clients[name]

    backend = backend_of(
        world,
        client_for=client_for,
        accounts=lambda: accounts_of(world),
        activate=lambda group, profile: active.update({group: profile}),
    )

    async def scenario(ui):
        before = ui.static("#who")
        await ui.press("p")
        ui.app.screen.query_one("#accounts", DataTable).move_cursor(row=2)
        await ui.press("enter")
        return before, ui.static("#who")

    before, after = run_ui(backend, scenario)

    assert "svc-finance (user)" in before and "ana (user)" not in before
    assert "ana (user)" in after and "svc-finance (user)" not in after


def test_the_button_does_the_same_as_enter(world):
    world.run("groups")
    activated = []
    backend = backend_of(
        world,
        accounts=lambda: accounts_of(world),
        activate=lambda group, profile: activated.append((group, profile)),
    )

    async def scenario(ui):
        await ui.press("p")
        ui.app.screen.query_one("#accounts", DataTable).move_cursor(row=1)
        await ui.click("#activate")

    run_ui(backend, scenario)

    assert activated == [("user", "svc-finance")]


def test_n_stores_a_new_token_for_the_highlighted_profile(world):
    world.run("groups")
    backend = backend_of(world, accounts=lambda: accounts_of(world))

    async def scenario(ui):
        await ui.press("p")
        ui.app.screen.query_one("#accounts", DataTable).move_cursor(row=2)
        await ui.press("n")
        modal = ui.app.screen
        return (
            isinstance(modal, SignInModal),
            modal._selected_group(),
            modal.query_one("#profile", Input).value,
        )

    opened, group, profile = run_ui(backend, scenario)

    assert opened and group == "user" and profile == "ana"


def test_a_session_that_cannot_switch_profiles_says_so(world):
    world.run("groups")
    backend = backend_of(world, accounts=lambda: accounts_of(world))  # no activate

    async def scenario(ui):
        await ui.press("p")
        await ui.press("enter")
        return messages(ui)

    notes = run_ui(backend, scenario)

    assert any("cannot switch profiles" in n for n in notes)


def test_a_switch_that_fails_is_said_and_nothing_else_happens(world):
    world.run("groups")

    def broken(group, profile):
        raise RuntimeError("the settings are read-only")

    backend = backend_of(world, accounts=lambda: accounts_of(world), activate=broken)

    async def scenario(ui):
        await ui.press("p")
        await ui.press("enter")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert screen == "ExplorerScreen"
    assert any("Cannot switch: the settings are read-only" in n for n in notes)


def test_the_dialog_gives_up_on_escape(world):
    world.run("groups")
    activated = []
    backend = backend_of(
        world,
        accounts=lambda: accounts_of(world),
        activate=lambda group, profile: activated.append((group, profile)),
    )

    async def scenario(ui):
        await ui.press("p")
        await ui.press("escape")
        return type(ui.app.screen).__name__

    assert run_ui(backend, scenario) == "ExplorerScreen" and activated == []


def test_the_dialog_is_not_offered_on_a_lake_that_is_only_looked_at(world, tmp_path):
    from pbi_cli.core.store import LakeStore

    world.run("groups")
    backend = backend_of(
        world,
        store=LakeStore(world.store.root, readonly=True, reason="opened with --lake"),
        accounts=lambda: accounts_of(world),
    )

    async def scenario(ui):
        await ui.press("p")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert screen == "ExplorerScreen" and any("opened with --lake" in n for n in notes)


def test_the_dialog_is_in_the_palette(world):
    world.run("groups")

    async def scenario(ui):
        return "Accounts…" in {c.title for c in commands_for(ui.app, ui.app.screen)}

    assert run_ui(backend_of(world), scenario) is True
