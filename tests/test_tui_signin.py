"""Signing in from the TUI, with the clients of a real pool: what a pasted token does.

The tokens are kept in a dictionary that the clients read when a pool makes them, as the
command line does with the keyring, so that a token stored while the TUI runs is picked up by
the run that goes on, and by nothing else.
"""

import contextlib
from datetime import timedelta

import pytest
from core_helpers import make_client, make_token
from sync_helpers import TENANT, World
from tui_helpers import backend_of, run_ui

from pbi_cli.cli_support import ClientPool
from pbi_cli.core.planfile import PlanFile
from pbi_cli.core.registry import Scope
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.errors import AuthError, TokenExpiredError
from pbi_cli.tui.app import PBIApp
from pbi_cli.tui.explorer import workspace_of
from pbi_cli.tui.modals import SignInModal
from pbi_cli.tui.run import RunState

PLAN = """\
version: 1
accounts: {admin: adm, user: [service]}
tenant: {targets: [groups, reports, datasets, dashboards, dataflows]}
workspaces:
  - {id: ws-0001, details: [users, pages], via: auto}
"""

#: The profile that is the active one of each kind.
ACTIVE = {"admin": "adm", "user": "service"}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def token(world, hours, oid):
    """A token that expires in ``hours`` (before now, when negative), as the fake clock sees it."""
    return make_token(
        tenant=TENANT,
        expires_in=timedelta(hours=hours),
        now=world.clock.now(),
        oid=oid,
        admin=oid == "oid-adm" or None,
    )


class Keyring:
    """The stored tokens, and a pool of clients that reads them when it makes a client."""

    def __init__(self, world, **tokens):
        self.world = world
        self.tokens = dict(tokens)
        self.pool = ClientPool(self._make)
        self.stored = []

    @contextlib.contextmanager
    def _make(self, group, profile=None):
        name = profile or ACTIVE[group]
        client, _ = make_client(
            self.world.fake,
            clock=self.world.clock,
            store=self.world.store,
            token=self.tokens[name],
            profile=name,
            group=group,
        )
        yield client

    def sign_in(self, token, profile, group):
        self.stored.append((profile, group))
        self.tokens[profile] = token
        self.pool.reset()


def backend_with(world, tmp_path, keyring, plan_text=PLAN):
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(plan_text, encoding="utf-8")
    return backend_of(
        world,
        plan=PlanFile.load(path),
        reload_plan=lambda: PlanFile.load(path),
        client_for=keyring.pool,
        sign_in=keyring.sign_in,
        active_profile=lambda group: ACTIVE[group],
    )


def ask(ui, group="user", profile="service", reason=""):
    ui.app.action_sign_in(reason=reason, group=group, profile=profile)


async def run_plan(ui):
    await ui.press("s")
    await ui.until(lambda: ui.sync.plans > 0)
    await ui.settle()
    await ui.press("r")
    await ui.finish_sync()


async def paste_and_submit(ui, text):
    modal = ui.app.screen
    assert isinstance(modal, SignInModal)
    modal.query_one("#token").value = text
    await ui.press("enter")


async def dialog(ui, not_this_one=None):
    """The sign-in dialog that is open (another than the one given, when one is)."""
    await ui.until(
        lambda: isinstance(ui.app.screen, SignInModal)
        and ui.app.screen is not not_this_one
    )
    await ui.settle()  # until it has been drawn: the app may be closed right after
    return ui.app.screen


# ---------------------------------------------------------------------------
# a sign-in that takes
# ---------------------------------------------------------------------------


def test_one_expired_user_token_asks_once_and_the_plan_goes_on(world, tmp_path):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, -1, "oid-service")
    )
    asked = []

    async def scenario(ui):
        await run_plan(ui)
        modal = await dialog(ui)
        asked.append((modal._group, modal._profile, modal._reason))
        await paste_and_submit(ui, token(world, 1, "oid-service"))
        await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))
        await ui.finish_sync()
        return ui.app.run_state.report.status, isinstance(ui.app.screen, SignInModal)

    status, asking_again = run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert asked == [
        (
            "user",
            "service",
            "The token for profile 'service' expired at 2026-09-30 11:00 UTC. Sign in "
            "again and store a fresh token with `pbi auth -t <token> -p service -g user`.",
        )
    ]
    assert keyring.stored == [("service", "user")]
    assert status == "completed" and not asking_again


def test_an_administrators_token_that_expired_is_asked_for_as_the_administrators(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, -1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        await run_plan(ui)
        modal = await dialog(ui)
        return modal._group, modal._profile

    assert run_ui(backend_with(world, tmp_path, keyring), scenario) == ("admin", "adm")


# ---------------------------------------------------------------------------
# a token that cannot be of use
# ---------------------------------------------------------------------------


def test_a_token_that_has_expired_already_is_not_stored(world, tmp_path):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        ask(ui)
        await ui.settle()
        await paste_and_submit(ui, token(world, -2, "oid-service"))
        modal = ui.app.screen
        return isinstance(modal, SignInModal), ui.static("#error")

    still_open, error = run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert still_open
    assert error == "That token expired at 2026-09-30 10:00 UTC. Paste a fresh one."
    assert keyring.stored == []


def test_a_token_that_expires_within_the_seconds_a_request_needs_is_not_stored(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )
    short = make_token(
        tenant=TENANT,
        expires_in=timedelta(seconds=10),
        now=world.clock.now(),
        oid="oid-service",
    )

    async def scenario(ui):
        ask(ui)
        await ui.settle()
        await paste_and_submit(ui, short)
        return isinstance(ui.app.screen, SignInModal)

    assert run_ui(backend_with(world, tmp_path, keyring), scenario) is True
    assert keyring.stored == []


def test_a_token_that_is_submitted_twice_is_stored_and_answered_once(world, tmp_path):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        screens = len(ui.app.screen_stack)
        ask(ui)
        await ui.settle()
        modal = ui.app.screen
        modal.query_one("#token").value = token(world, 1, "oid-service")
        modal._sign_in()  # Enter ...
        modal._sign_in()  # ... and a click on the button, before the dialog is gone
        await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))
        await ui.settle()
        signed = [n.message for n in ui.app._notifications if "Signed in" in n.message]
        return signed, len(ui.app.screen_stack) - screens

    signed, extra_screens = run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert keyring.stored == [("service", "user")]
    assert signed == ["Signed in (user)."]
    assert extra_screens == 0  # the screen below was not dismissed with it


def test_a_token_that_says_nothing_about_its_expiry_is_stored(world, tmp_path):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        ask(ui)
        await ui.settle()
        await paste_and_submit(ui, "not-a-jwt-but-the-api-decides")
        await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))

    run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert keyring.stored == [("service", "user")]


def test_a_token_that_is_refused_right_after_it_was_stored_is_said_to_be(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )
    world.fake.fail("GET", r"^/groups$", 401)  # the service does not take the token
    reasons = []

    async def scenario(ui):
        await run_plan(ui)
        previous = None
        for _ in range(3):
            previous = await dialog(ui, not_this_one=previous)
            reasons.append(previous._reason)
            await paste_and_submit(ui, token(world, 1, "oid-service"))

    run_ui(backend_with(world, tmp_path, keyring), scenario)

    first, second, third = reasons
    assert first.startswith("Power BI rejected the token (401 Unauthorized)")
    for again in (second, third):
        assert again.startswith(
            "The token that was just stored for service (user) was refused as well. "
            "Power BI rejected the token (401 Unauthorized)"
        )


def test_another_account_that_has_run_out_is_not_called_a_refused_token(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, -1, "oid-adm"), service=token(world, -1, "oid-service")
    )
    asked = []

    async def scenario(ui):
        await run_plan(ui)
        first = await dialog(ui)
        asked.append((first._group, first._profile, first._reason))
        await paste_and_submit(ui, token(world, 1, "oid-adm"))
        second = await dialog(ui, not_this_one=first)
        asked.append((second._group, second._profile, second._reason))

    run_ui(backend_with(world, tmp_path, keyring), scenario)

    (group1, profile1, _), (group2, profile2, reason2) = asked
    assert (group1, profile1) == ("admin", "adm")
    assert (group2, profile2) == ("user", "service")
    assert reason2.startswith("The token for profile 'service' expired at")


def test_another_account_of_the_same_kind_is_not_called_a_refused_token(
    world, tmp_path
):
    keyring = Keyring(
        world,
        adm=token(world, 1, "oid-adm"),
        service=token(world, -1, "oid-service"),
        other=token(world, -1, "oid-other"),
    )
    plan = (
        "version: 1\naccounts: {admin: adm, user: [service, other]}\n"
        "tenant: {targets: [user-groups]}\n"
    )
    asked = []

    async def scenario(ui):
        await run_plan(ui)
        first = await dialog(ui)
        asked.append((first._profile, first._reason))
        await paste_and_submit(ui, token(world, 1, "oid-service"))
        second = await dialog(ui, not_this_one=first)
        asked.append((second._profile, second._reason))

    run_ui(backend_with(world, tmp_path, keyring, plan), scenario)

    (profile1, _), (profile2, reason2) = asked
    assert (profile1, profile2) == ("service", "other")
    assert reason2.startswith("The token for profile 'other' expired at")


def test_a_refusal_with_no_profile_is_said_for_the_kind_that_was_signed_in_only(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        reasons = []
        for kind in ("user", "admin"):
            ui.app.explain_sync_problem(
                AuthError("nothing is stored", group=kind), None, ("admin", "adm")
            )
            modal = await dialog(ui)
            reasons.append((modal._group, modal._reason))
            await ui.press("escape")
            await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))
        return reasons

    assert run_ui(backend_with(world, tmp_path, keyring), scenario) == [
        ("user", "nothing is stored"),
        (
            "admin",
            "The token that was just stored for adm (admin) was refused as well. "
            "nothing is stored",
        ),
    ]


def test_a_sync_that_cannot_start_after_a_sign_in_says_the_token_did_not_do(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, 1, "oid-service")
    )

    async def scenario(ui):
        state = RunState("Plan", SyncOptions(), world.clock.now())
        ui.app._signed_in_as = ("user", "service")
        ui.app._sync_done(
            state,
            None,
            TokenExpiredError("No credentials", group="user", profile="service"),
        )
        modal = await dialog(ui)
        return modal._reason, ui.app._signed_in_as

    reason, remembered = run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert reason == (
        "The token that was just stored for service (user) was refused as well. "
        "No credentials"
    )
    assert remembered is None


def test_a_token_that_runs_out_later_is_asked_for_as_if_it_was_the_first_time(
    world, tmp_path
):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, -1, "oid-service")
    )
    reasons = []

    async def scenario(ui):
        await run_plan(ui)
        reasons.append((await dialog(ui))._reason)
        await paste_and_submit(ui, token(world, 1, "oid-service"))
        await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))
        await ui.finish_sync()
        world.clock.advance(hours=2)  # both tokens run out, and everything is old
        await ui.press("r")
        await ui.finish_sync()
        reasons.append((await dialog(ui))._reason)

    run_ui(backend_with(world, tmp_path, keyring), scenario)

    assert not reasons[1].startswith("The token that was just stored")
    assert "expired at" in reasons[1]


def test_a_dialog_that_is_cancelled_forgets_what_it_was_asked_for(world, tmp_path):
    keyring = Keyring(
        world, adm=token(world, 1, "oid-adm"), service=token(world, -1, "oid-service")
    )

    async def scenario(ui):
        await run_plan(ui)
        await dialog(ui)
        await ui.press("escape")
        return ui.app._resume, ui.app._signed_in_as

    assert run_ui(backend_with(world, tmp_path, keyring), scenario) == (None, None)


# ---------------------------------------------------------------------------
# the accounts of a plan file
# ---------------------------------------------------------------------------


def two_users(world):
    return Keyring(
        world,
        adm=token(world, 1, "oid-adm"),
        service=token(world, 1, "oid-service"),
        other=token(world, -1, "oid-other"),
    )


def test_a_session_works_with_the_accounts_its_plan_names(world, tmp_path):
    keyring = two_users(world)
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {admin: adm, user: [service, other]}\n"
        "tenant: {targets: [groups]}\n",
    )

    assert backend.account_slots() == [
        (Scope.ADMIN, "adm"),
        (Scope.USER, "service"),
        (Scope.USER, "other"),
    ]
    assert [(i.group, i.profile) for i in backend.identities()] == [
        ("admin", "adm"),
        ("user", "service"),
        ("user", "other"),
    ]


def test_without_a_plan_or_where_it_names_none_the_active_profiles_are_used(
    world, tmp_path
):
    keyring = two_users(world)
    bare = backend_with(
        world, tmp_path, keyring, "version: 1\ntenant: {targets: [groups]}\n"
    )
    plain = backend_of(world)

    assert bare.account_slots() == [(Scope.ADMIN, None), (Scope.USER, None)]
    assert plain.account_slots() == [(Scope.ADMIN, None), (Scope.USER, None)]
    assert [(i.group, i.profile) for i in bare.identities()] == [
        ("admin", "adm"),
        ("user", "service"),
    ]


def test_an_account_of_the_plan_with_no_token_is_left_out_of_who_is_signed_in(
    world, tmp_path
):
    keyring = two_users(world)
    del keyring.tokens["other"]
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {user: [other, service]}\ntenant: {targets: [groups]}\n",
    )

    assert [(i.group, i.profile) for i in backend.identities()] == [
        ("admin", "adm"),
        ("user", "service"),
    ]


def test_the_profiles_a_fetch_is_made_through_are_those_of_the_plan(world, tmp_path):
    keyring = two_users(world)
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {admin: adm, user: [service, other]}\n"
        "tenant: {targets: [groups]}\n",
    )

    assert backend.profiles_for() == ("adm", "service")  # the first when none lists it
    assert backend.profiles_for(["other"]) == ("adm", "other")
    assert backend.profiles_for(["nobody", "other", "service"]) == ("adm", "service")
    assert backend_of(world).profiles_for(["other"]) == (None, None)


def test_a_plan_that_names_no_user_account_fetches_as_the_active_one(world, tmp_path):
    keyring = two_users(world)
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {admin: adm}\ntenant: {targets: [groups]}\n",
    )

    assert backend.profiles_for(["service"]) == ("adm", None)


def test_the_header_shows_each_account_of_the_plan(world, tmp_path):
    keyring = two_users(world)
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {admin: adm, user: [service, other]}\n"
        "tenant: {targets: [groups]}\n",
    )

    async def scenario(ui):
        return ui.static("#who")

    header = run_ui(backend, scenario)

    assert "adm (admin)" in header and "service (user)" in header
    assert "other (user)" in header


def test_f_fetches_through_the_user_account_of_the_plan_that_lists_the_workspace(
    world, tmp_path, monkeypatch
):
    world.run()
    keyring = Keyring(
        world,
        adm=token(world, 1, "oid-adm"),
        service=token(world, 1, "oid-service"),
        other=token(world, 1, "oid-other"),
    )
    world.fake.visible_to = {"oid-service": ["ws-0002"], "oid-other": ["ws-0001"]}
    backend = backend_with(
        world,
        tmp_path,
        keyring,
        "version: 1\naccounts: {admin: adm, user: [service, other]}\n"
        "tenant: {targets: [groups]}\n",
    )
    for profile in ("service", "other"):  # each account lists its own workspaces
        backend.engine().run(
            SyncOptions(targets=("user-groups",), user_profile=profile, workers=1)
        )
    started = []
    monkeypatch.setattr(
        PBIApp, "start_sync", lambda app, work, label: started.append(work) or True
    )

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("dataset:ds-0001")
        await ui.press("6")
        await ui.press("f")
        await ui.press("enter")
        return [(o.admin_profile, o.user_profile) for o in started]

    assert run_ui(backend, scenario) == [("adm", "other")]


def test_the_workspace_of_a_subject_is_known_for_a_workspace_and_an_item(world):
    world.run()
    from pbi_cli.core.catalog import Catalog

    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    assert workspace_of(catalog.workspace("ws-0001")) == "ws-0001"
    assert workspace_of(catalog.item("report", "rep-0001")) == "ws-0001"
    assert workspace_of(None) is None
    assert workspace_of("something else") is None
