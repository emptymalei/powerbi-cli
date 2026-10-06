"""The plan file: reading it, every way it can be wrong, and the sync runs it makes."""

from datetime import timedelta
from pathlib import Path

import pytest

from pbi_cli.core.catalog import Workspace
from pbi_cli.core.planfile import (
    Accounting,
    Overrides,
    PlanFile,
    PlanFileError,
    describe_scan,
    find_workspace,
)
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.plan import MAX_DAYS, SyncOptions
from pbi_cli.core.sync.targets import select_targets

FILE = Path("/work/pbi-plan.yaml")


def read(text: str) -> PlanFile:
    return PlanFile.parse(text, FILE)


def refused(text: str) -> str:
    """The message of the error a plan file gives."""
    with pytest.raises(PlanFileError) as error:
        read(text)
    return str(error.value)


def workspace(name, ident=None, visible_to=(), kind="Workspace", state="Active"):
    return Workspace(
        id=ident or name.lower().replace(" ", "-"),
        name=name,
        type=kind,
        state=state,
        raw={},
        visible_to=list(visible_to),
    )


class Stored:
    """Which accounts are stored: the answers the sync engine gives about them."""

    def __init__(self, admin=("adm",), users=("ana", "bob"), active_user="ana"):
        self.admin = tuple(admin)
        self.users = tuple(users)
        self.active = {Scope.ADMIN: self.admin[0] if self.admin else None}
        self.active[Scope.USER] = active_user if self.users else None

    def has_account(self, scope, profile=None):
        profiles = self.admin if scope is Scope.ADMIN else self.users
        return (profile or self.active[scope]) in profiles

    def profile_of(self, scope, profile=None):
        found = profile or self.active[scope]
        return found if self.has_account(scope, found) else None


# ---------------------------------------------------------------------------
# reading a file
# ---------------------------------------------------------------------------

EXAMPLE = """\
version: 1
accounts:
  admin: adm
  user: [ana, bob]
tenant:
  targets: [default, activity]
  activity_days: 7
workspaces:
  - name: "Finance*"
    scan: {lineage: true, datasource_details: true}
    details: [users, datasources]
    via: auto
  - id: ws-1
    details: [pages]
    via: ana
session:
  lake: ../shared
  open: Finance
  lazy: auto
"""


def test_a_plan_is_read():
    plan = read(EXAMPLE)

    assert plan.version == 1
    assert plan.accounts.admin == "adm" and plan.accounts.user == ("ana", "bob")
    assert plan.tenant.targets == ("default", "activity")
    assert plan.tenant.activity_days == 7
    first, second = plan.workspaces
    assert (first.name, first.id, first.via) == ("Finance*", "", "auto")
    assert first.scan == ScanFlags(lineage=True, datasource_details=True)
    assert first.details == ("users", "datasources")
    assert (second.id, second.name, second.scan, second.via) == (
        "ws-1",
        "",
        None,
        "ana",
    )
    assert second.details == ("pages",)
    assert plan.session.open == "Finance" and plan.session.lazy == "auto"
    assert plan.path == FILE and plan.name == "pbi-plan.yaml"


def test_a_plan_that_says_only_its_version_is_an_empty_plan():
    plan = read("version: 1\n")

    assert plan.accounts.admin is None and plan.accounts.user == ()
    assert plan.tenant is None and plan.workspaces == ()
    assert plan.session.lake is None and plan.session.open is None
    assert plan.session.lazy == "ask"


@pytest.mark.parametrize(
    "written, mode",
    [("ask", "ask"), ("auto", "auto"), ("off", "off"), ('"off"', "off"), ("no", "off")],
)
def test_the_modes_of_lazy_are_read_as_they_are_written(written, mode):
    # YAML reads a bare off (and no) as false, which must not make "off" unusable
    assert read(f"version: 1\nsession: {{lazy: {written}}}\n").session.lazy == mode


def test_a_mode_of_lazy_that_is_true_is_not_a_mode():
    assert "session.lazy: must be text" in refused("version: 1\nsession: {lazy: on}\n")


def test_a_section_with_nothing_under_it_is_an_empty_section():
    plan = read("version: 1\naccounts:\nworkspaces:\nsession:\n")

    assert plan.accounts.admin is None and plan.accounts.user == ()
    assert plan.workspaces == ()
    assert plan.session.lazy == "ask" and plan.session.lake is None


def test_a_tenant_with_nothing_in_it_is_the_plain_sync():
    for text in ("version: 1\ntenant:\n", "version: 1\ntenant: {}\n"):
        tenant = read(text).tenant
        assert tenant is not None and tenant.targets == ()
        assert tenant.activity_days is None and tenant.scan == ScanFlags()


def test_scan_true_is_a_scan_with_no_options_and_false_is_no_scan():
    plan = read(
        "version: 1\nworkspaces:\n"
        "  - {id: a, scan: true}\n"
        "  - {id: b, scan: false, details: [users]}\n"
    )

    assert plan.workspaces[0].scan == ScanFlags()
    assert plan.workspaces[1].scan is None


def test_the_scan_options_are_those_of_the_scan_config_file():
    plan = read(
        "version: 1\nworkspaces:\n  - id: a\n    scan:\n"
        "      lineage: true\n      datasource_details: true\n"
        "      dataset_schema: true\n      dataset_expressions: true\n"
        "      get_artifact_users: true\n"
    )

    assert plan.workspaces[0].scan == ScanFlags(True, True, True, True, True)


@pytest.mark.parametrize(
    "text, key",
    [
        ("version: 1\naccounts: {admin: ''}\n", "accounts.admin"),
        ("version: 1\naccounts: {admin: '   '}\n", "accounts.admin"),
        (
            "version: 1\nworkspaces:\n  - {name: ' ', scan: true}\n",
            "workspaces[0].name",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, scan: true, via: ''}\n",
            "workspaces[0].via",
        ),
        ("version: 1\nsession: {open: ''}\n", "session.open"),
        ("version: 1\nsession: {lake: ' '}\n", "session.lake"),
    ],
)
def test_text_cannot_be_empty(text, key):
    assert f"{key}: must be text" in refused(text)


def test_text_is_trimmed():
    plan = read(
        "version: 1\naccounts: {admin: ' adm ', user: [' ana ', bob]}\n"
        "workspaces:\n  - {name: '  Finance*  ', scan: true, via: ' ana '}\n"
        "  - {id: ' ws-1 ', scan: true}\n"
        "session: {open: ' Fin ', lake: ' /lake '}\n"
    )

    assert plan.accounts.admin == "adm" and plan.accounts.user == ("ana", "bob")
    assert plan.workspaces[0].name == "Finance*" and plan.workspaces[0].via == "ana"
    assert plan.workspaces[1].id == "ws-1"
    assert plan.session.open == "Fin" and plan.session.lake == "/lake"


@pytest.mark.parametrize("days", [1, 28])
def test_the_days_of_events_run_from_one_to_twenty_eight(days):
    plan = read(
        f"version: 1\ntenant:\n  targets: [activity]\n  activity_days: {days}\n"
    )

    assert plan.tenant.activity_days == days


def test_a_detail_that_is_listed_twice_counts_once():
    plan = read(
        "version: 1\nworkspaces:\n  - {id: a, details: [users, users, tiles]}\n"
    )

    assert plan.workspaces[0].details == ("users", "tiles")


def test_the_workspaces_of_the_file_know_where_they_are():
    plan = read(EXAMPLE)

    assert [w.index for w in plan.workspaces] == [0, 1]
    assert plan.workspaces[0].line == 9 and plan.workspaces[1].line == 13
    assert plan.workspaces[0].where == "workspaces[0] (Finance*)"
    assert plan.workspaces[1].label == "ws-1"
    assert plan.workspaces[1].names_a_profile and not plan.workspaces[0].names_a_profile


def test_an_id_may_come_with_a_name_that_is_only_its_label():
    # as in the file of `pbi workspaces scan batch`: an id, and a name to tell it by
    plan = read(
        "version: 1\nworkspaces:\n"
        "  - {id: ws-1, name: IT Management, scan: true}\n"
        "  - {id: ws-2, scan: true}\n  - {name: 'Fin*', scan: true}\n"
    )

    first, second, third = plan.workspaces
    assert (first.id, first.name) == ("ws-1", "IT Management")
    assert first.label == "IT Management"  # what a message calls it
    assert first.where == "workspaces[0] (IT Management)"
    assert (second.label, third.label) == ("ws-2", "Fin*")  # the id, else the pattern


def test_the_scan_flags_are_described_in_words():
    assert describe_scan(ScanFlags()) == "no options"
    assert (
        describe_scan(ScanFlags(lineage=True, dataset_schema=True))
        == "lineage, dataset schema"
    )


# ---------------------------------------------------------------------------
# every way a file can be wrong: the message names the file, the line and the key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", "pbi-plan.yaml: the file is empty: a plan starts with `version: 1`"),
        ("[a, b]\n", "pbi-plan.yaml: must be a mapping (key: value)"),
        (
            "version: 1\n\tworkspaces: []\n",
            "pbi-plan.yaml:2: not valid YAML: found character '\\t' that cannot start "
            "any token",
        ),
        ("workspaces: []\n", "pbi-plan.yaml: version: missing: a plan starts with"),
        ("version: one\n", "pbi-plan.yaml:1: version: must be a whole number"),
        (
            "version: 2\n",
            "pbi-plan.yaml:1: version: 2 is not a version this pbi reads (it reads 1)",
        ),
        (
            "version: 1\nworkspace: []\n",
            "pbi-plan.yaml:2: workspace: unknown key. Did you mean 'workspaces'? "
            "The keys here are: version, accounts, tenant, workspaces, session.",
        ),
        (
            "version: 1\nversion: 1\n",
            "pbi-plan.yaml:2: not valid YAML: found the key 'version' twice",
        ),
        (
            "version: 1\naccounts: admin\n",
            "pbi-plan.yaml:2: accounts: must be a mapping",
        ),
        (
            "version: 1\naccounts:\n  admins: x\n",
            "pbi-plan.yaml:3: accounts.admins: unknown key. Did you mean 'admin'?",
        ),
        (
            "version: 1\naccounts:\n  admin: 5\n",
            "pbi-plan.yaml:3: accounts.admin: must be text (put it in quotes)",
        ),
        (
            "version: 1\naccounts:\n  user: ana\n",
            "pbi-plan.yaml:3: accounts.user: must be a list, for example [a, b]",
        ),
        (
            "version: 1\naccounts:\n  user: [ana, 3]\n",
            "pbi-plan.yaml:3: accounts.user[1]: must be text",
        ),
        (
            "version: 1\ntenant:\n  targets: [activty]\n",
            "pbi-plan.yaml:3: tenant.targets[0]: 'activty' is not a target. "
            "Did you mean 'activity'?",
        ),
        (
            "version: 1\ntenant:\n  targets: [activity]\n  activity_days: 0\n",
            "pbi-plan.yaml:4: tenant.activity_days: must be from 1 to 28",
        ),
        (
            "version: 1\ntenant:\n  targets: [activity]\n  activity_days: 29\n",
            "pbi-plan.yaml:4: tenant.activity_days: must be from 1 to 28",
        ),
        (
            "version: 1\ntenant:\n  targets: [activity]\n  activity_days: '7'\n",
            "pbi-plan.yaml:4: tenant.activity_days: must be a whole number",
        ),
        (
            "version: 1\ntenant:\n  targets: [activity]\n  activity_days: true\n",
            "pbi-plan.yaml:4: tenant.activity_days: must be a whole number",
        ),
        (
            "version: 1\ntenant:\n  activity_days: 7\n",
            "pbi-plan.yaml:3: tenant.activity_days: has no effect: 'activity' is not "
            "among tenant.targets",
        ),
        (
            "version: 1\ntenant:\n  full_scan: true\n",
            "pbi-plan.yaml:3: tenant.full_scan: has no effect",
        ),
        (
            "version: 1\ntenant:\n  exclude_personal: true\n",
            "pbi-plan.yaml:3: tenant.exclude_personal: has no effect",
        ),
        (
            "version: 1\ntenant:\n  exclude_inactive: true\n",
            "pbi-plan.yaml:3: tenant.exclude_inactive: has no effect",
        ),
        (
            "version: 1\ntenant:\n  targets: [scan]\n  scan: {lineages: true}\n",
            "pbi-plan.yaml:4: tenant.scan.lineages: unknown key. Did you mean 'lineage'?",
        ),
        (
            "version: 1\ntenant:\n  targets: [scan]\n  scan: {lineage: 1}\n",
            "pbi-plan.yaml:4: tenant.scan.lineage: must be true or false",
        ),
        (
            "version: 1\ntenant:\n  targets: [scan]\n  full_scan: maybe\n",
            "pbi-plan.yaml:4: tenant.full_scan: must be true or false",
        ),
        ("version: 1\nworkspaces: a\n", "pbi-plan.yaml:2: workspaces: must be a list"),
        (
            "version: 1\nworkspaces:\n  - a\n",
            "pbi-plan.yaml:3: workspaces[0]: must be a mapping",
        ),
        (
            "version: 1\nworkspaces:\n  - {scan: true}\n",
            "pbi-plan.yaml:3: workspaces[0]: needs `id:` (one workspace) or `name:` "
            "(a pattern for names); an `id:` may come with a `name:` that is only its label",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a}\n",
            "pbi-plan.yaml:3: workspaces[0]: says nothing to keep: add `scan:`",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, scan: false}\n",
            "pbi-plan.yaml:3: workspaces[0]: says nothing to keep",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, scans: true}\n",
            "pbi-plan.yaml:3: workspaces[0].scans: unknown key. Did you mean 'scan'?",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, details: [users, page]}\n",
            "pbi-plan.yaml:3: workspaces[0].details[1]: 'page' is not a detail. "
            "Did you mean 'pages'? The details are: users, datasources, pages, "
            "refreshes, parameters, tiles.",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, details: users}\n",
            "pbi-plan.yaml:3: workspaces[0].details: must be a list",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, scan: true, via: 3}\n",
            "pbi-plan.yaml:3: workspaces[0].via: must be text (put it in quotes)",
        ),
        (
            "version: 1\nworkspaces:\n  - {id: a, scan: {nope: true}}\n",
            "pbi-plan.yaml:3: workspaces[0].scan.nope: unknown key",
        ),
        (
            "version: 1\nsession:\n  lazy: sometimes\n",
            "pbi-plan.yaml:3: session.lazy: 'sometimes' is not a mode. The modes are: "
            "ask, auto, off.",
        ),
        (
            "version: 1\nsession:\n  laze: ask\n",
            "pbi-plan.yaml:3: session.laze: unknown key. Did you mean 'lazy'?",
        ),
        (
            "version: 1\nsession:\n  lake: 7\n",
            "pbi-plan.yaml:3: session.lake: must be text",
        ),
        ("version: 1\nsession: ask\n", "pbi-plan.yaml:2: session: must be a mapping"),
    ],
)
def test_a_mistake_in_the_file_says_where_and_what(text, expected):
    assert expected in refused(text)


def test_the_line_of_a_mistake_in_a_list_is_the_line_of_the_entry():
    message = refused(
        "version: 1\nworkspaces:\n"
        "  - id: a\n    details: [users]\n"
        "  - id: b\n    details: [users, nonsense]\n"
    )

    assert message.startswith("pbi-plan.yaml:6: workspaces[1].details[1]:")


def test_a_plan_that_is_not_from_a_file_is_called_the_plan():
    with pytest.raises(PlanFileError, match=r"^the plan: version: missing"):
        PlanFile.parse("workspaces: []")


def test_a_missing_file_is_said_in_words(tmp_path):
    with pytest.raises(PlanFileError, match="Cannot read the plan file .*nope.yaml"):
        PlanFile.load(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le-bom", "utf-16-be-bom"]
)
def test_a_file_is_read_as_the_tool_that_made_it_wrote_it(tmp_path, encoding):
    import codecs

    text = "version: 1\nsession: {open: 'Zürich*'}\n"
    data = {
        "utf-8": text.encode("utf-8"),
        "utf-8-sig": text.encode("utf-8-sig"),
        "utf-16": text.encode(
            "utf-16"
        ),  # Python writes the mark and the machine's order
        "utf-16-le-bom": codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
        "utf-16-be-bom": codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
    }[encoding]
    path = tmp_path / "plan.yaml"
    path.write_bytes(data)

    assert PlanFile.load(path).session.open == "Zürich*"


@pytest.mark.parametrize(
    "data",
    [
        b"version: 1\n\xc3\x28",  # not UTF-8
        b"\xff\xfev\x00e",  # UTF-16 that stops in the middle of a character
    ],
)
def test_a_file_that_is_not_text_is_said_in_words(tmp_path, data):
    path = tmp_path / "plan.yaml"
    path.write_bytes(data)

    with pytest.raises(PlanFileError, match="Cannot read the plan file"):
        PlanFile.load(path)


def test_a_file_is_loaded_with_its_place(tmp_path):
    path = tmp_path / "sub" / "plan.yaml"
    path.parent.mkdir()
    path.write_text("version: 1\nsession: {lake: data}\n", encoding="utf-8")

    plan = PlanFile.load(path)

    assert plan.path == path.resolve()
    assert plan.lake == str(path.resolve().parent / "data")


def test_a_yaml_file_cannot_run_code():
    message = refused(
        "version: 1\nsession:\n  open: !!python/object/apply:os.system [x]\n"
    )

    assert "not valid YAML" in message


# ---------------------------------------------------------------------------
# where the lake of a session is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wanted, expected",
    [
        ("s3://bucket/lake", "s3://bucket/lake"),
        ("az://container/lake", "az://container/lake"),
        ("/shared/lake", "/shared/lake"),
        ("shared", "/work/shared"),
        ("../shared", "/work/../shared"),
    ],
)
def test_the_lake_of_a_plan_is_a_url_or_a_folder_from_the_folder_of_the_file(
    wanted, expected
):
    assert read(f"version: 1\nsession: {{lake: '{wanted}'}}\n").lake == expected


def test_a_home_folder_is_expanded():
    found = read("version: 1\nsession: {lake: '~/lake'}\n").lake

    assert found == str(Path("~/lake").expanduser())
    assert "~" not in found


def test_a_plan_that_is_not_from_a_file_keeps_a_relative_lake_as_it_is():
    assert PlanFile.parse("version: 1\nsession: {lake: shared}\n").lake == "shared"
    assert PlanFile.parse("version: 1\n").lake is None


def test_the_profiles_a_file_names_come_in_order_without_repeats():
    plan = read(
        "version: 1\naccounts: {admin: adm, user: [ana, bob]}\n"
        "workspaces:\n  - {id: a, scan: true, via: cy}\n"
        "  - {id: b, scan: true, via: ana}\n  - {id: c, scan: true, via: auto}\n"
        "  - {id: d, scan: true, via: admin}\n"
    )

    assert plan.profiles == ["adm", "ana", "bob", "cy"]


# ---------------------------------------------------------------------------
# which accounts a plan uses
# ---------------------------------------------------------------------------


def test_the_accounts_are_the_named_profiles():
    accounting = read(
        "version: 1\naccounts: {admin: adm2, user: [bob, ana]}\n"
    ).accounting(Stored(admin=("adm", "adm2")))

    assert accounting == Accounting(admin="adm2", users=("bob", "ana"))
    assert accounting.available == {Scope.ADMIN, Scope.USER}


def test_without_a_word_about_accounts_the_active_profiles_are_used():
    accounting = read("version: 1\n").accounting(Stored(active_user="bob"))

    assert accounting == Accounting(admin="adm", users=("bob",))


def test_without_a_stored_account_there_are_none():
    accounting = read("version: 1\n").accounting(Stored(admin=(), users=()))

    assert accounting == Accounting(admin=None, users=())
    assert accounting.available == frozenset()
    assert Accounting(admin="a").available == {Scope.ADMIN}
    assert Accounting(users=("u",)).available == {Scope.USER}


def test_a_profile_that_has_no_token_is_said_with_the_command_that_stores_one():
    with pytest.raises(PlanFileError) as error:
        read("version: 1\naccounts: {admin: missing}\n").accounting(Stored())
    assert "accounts.admin: no token is stored under the profile 'missing'" in str(
        error.value
    )
    assert "pbi auth -t <token> -p missing -g admin" in str(error.value)

    with pytest.raises(PlanFileError) as error:
        read("version: 1\naccounts: {user: [ana, cy]}\n").accounting(Stored())
    assert "accounts.user[1]: no token is stored under the profile 'cy'" in str(
        error.value
    )
    assert "pbi auth -t <token> -p cy -g user" in str(error.value)

    with pytest.raises(PlanFileError) as error:
        read(
            "version: 1\nworkspaces:\n  - {id: a, scan: true}\n  - {id: b, details: "
            "[pages], via: cy}\n"
        ).accounting(Stored())
    assert "workspaces[1] (b).via: no token is stored under the profile 'cy'" in str(
        error.value
    )
    assert "pbi-plan.yaml:4:" in str(error.value)


# ---------------------------------------------------------------------------
# the steps for the tenant
# ---------------------------------------------------------------------------

BOTH = Accounting(admin="adm", users=("ana", "bob"))
ADMIN_ONLY = Accounting(admin="adm")
USER_ONLY = Accounting(users=("ana", "bob"))


def names(step):
    return sorted(step.options.targets)


def test_no_tenant_section_is_no_steps_for_the_tenant():
    assert read("version: 1\n").first_steps(BOTH) == []


def test_the_plain_tenant_is_the_lists_of_the_administrator():
    (step,) = read("version: 1\ntenant:\n").first_steps(BOTH)

    assert names(step) == sorted(select_targets((), {Scope.ADMIN}).names)
    assert step.options.admin_profile == "adm" and step.options.user_profile is None
    assert step.options.days == MAX_DAYS and step.title == "the tenant (adm)"


def test_the_events_come_with_their_days_and_the_scan_with_its_options():
    (step,) = read(
        "version: 1\ntenant:\n  targets: [default, activity, scan]\n"
        "  activity_days: 7\n  scan: {lineage: true}\n  full_scan: true\n"
        "  exclude_personal: true\n  exclude_inactive: true\n"
    ).first_steps(BOTH)

    assert {"activity", "scan", "groups"} <= set(step.options.targets)
    assert step.options.days == 7
    assert step.options.scan_flags == ScanFlags(lineage=True)
    assert step.options.full_scan
    assert step.options.exclude_personal and step.options.exclude_inactive


def test_with_only_user_accounts_the_tenant_is_each_users_own_lists():
    steps = read("version: 1\ntenant:\n").first_steps(USER_ONLY)

    assert [s.options.user_profile for s in steps] == ["ana", "bob"]
    assert all(s.options.admin_profile is None for s in steps)
    assert names(steps[0]) == sorted(select_targets((), {Scope.USER}).names)
    assert [s.title for s in steps] == ["the tenant (ana)", "the tenant (bob)"]


def test_the_targets_of_each_kind_of_account_are_run_with_that_account():
    steps = read(
        "version: 1\ntenant:\n  targets: [groups, user-groups, user-reports]\n"
    ).first_steps(BOTH)

    assert [(s.options.admin_profile, s.options.user_profile) for s in steps] == [
        ("adm", None),
        (None, "ana"),
        (None, "bob"),
    ]
    assert names(steps[0]) == ["groups"]
    assert names(steps[1]) == ["user-groups", "user-reports"]


def test_a_target_that_needs_an_account_that_is_not_stored_is_said_with_its_key():
    with pytest.raises(PlanFileError) as error:
        read("version: 1\ntenant:\n  targets: [activity]\n").first_steps(USER_ONLY)

    assert (
        "pbi-plan.yaml: tenant.targets: 'activity' needs an administrator account"
        in str(error.value)
    )


# ---------------------------------------------------------------------------
# what tells which user account lists which workspace
# ---------------------------------------------------------------------------


def workspaces_file(extra: str) -> PlanFile:
    return read(f"version: 1\nworkspaces:\n{extra}")


def test_the_workspaces_of_each_user_account_are_listed_first_when_that_is_needed():
    plan = workspaces_file("  - {name: 'F*', details: [pages], via: auto}\n")

    steps = plan.first_steps(BOTH)

    assert [(s.options.targets, s.options.user_profile) for s in steps] == [
        (("user-groups",), "ana"),
        (("user-groups",), "bob"),
    ]
    assert steps[0].title == "the workspaces of ana"


def test_an_administrator_can_say_who_has_access_so_no_user_list_is_needed():
    plan = workspaces_file(
        "  - {name: 'F*', details: [users, datasources], via: auto}\n"
    )

    assert plan.first_steps(BOTH) == []


def test_without_an_administrator_the_user_lists_are_needed_for_anything():
    plan = workspaces_file("  - {name: 'F*', details: [users], via: auto}\n")

    assert len(plan.first_steps(USER_ONLY)) == 2


def test_a_user_that_is_asked_for_needs_the_lists_and_a_named_profile_does_not():
    assert (
        len(
            workspaces_file("  - {name: F, details: [users], via: user}\n").first_steps(
                BOTH
            )
        )
        == 2
    )
    assert (
        workspaces_file("  - {name: F, details: [pages], via: ana}\n").first_steps(BOTH)
        == []
    )
    assert (
        workspaces_file("  - {name: F, scan: true, via: auto}\n").first_steps(BOTH)
        == []
    )
    assert (
        workspaces_file("  - {name: F, details: [pages], via: admin}\n").first_steps(
            BOTH
        )
        == []
    )


def test_a_list_that_a_tenant_step_fetches_anyway_is_not_asked_for_twice():
    plan = read(
        "version: 1\ntenant: {targets: [user-groups]}\n"
        "workspaces:\n  - {name: F, details: [pages]}\n"
    )

    steps = plan.first_steps(BOTH)

    assert [s.options.user_profile for s in steps] == ["ana", "bob"]
    assert all(s.title.startswith("the tenant") for s in steps)


def test_a_tenant_step_that_fetches_what_hangs_on_the_list_counts_too():
    plan = read(
        "version: 1\ntenant: {targets: [user-reports]}\n"
        "workspaces:\n  - {name: F, details: [pages]}\n"
    )

    assert [s.title for s in plan.first_steps(BOTH)] == [
        "the tenant (ana)",
        "the tenant (bob)",
    ]


# ---------------------------------------------------------------------------
# the steps for the workspaces
# ---------------------------------------------------------------------------

LAKE = [
    workspace("Finance EU", "ws-eu", visible_to=["ana"]),
    workspace("Finance US", "ws-us", visible_to=["bob"]),
    workspace("Finance Hidden", "ws-hid"),
    workspace("Sales", "ws-sales", visible_to=["ana", "bob"]),
    workspace("Finance Personal", "ws-pers", kind="PersonalGroup"),
    workspace("Finance Old", "ws-old", state="Deleted"),
]


class _Stand:
    """Something that has the id, the name and the lists of a workspace."""

    def __init__(self, ident):
        self.id = self.name = ident
        self.visible_to = []


def steps_for(extra: str, accounting=BOTH, lake=LAKE, listed=None):
    return workspaces_file(extra).workspace_steps(accounting, lake, listed)


def test_a_scan_step_is_made_of_the_workspaces_a_pattern_matches():
    compiled = steps_for("  - {name: 'Finance*', scan: {lineage: true}}\n")

    (step,) = compiled.steps
    assert step.options.targets == ("scan",)
    assert step.options.workspace_ids == ("ws-eu", "ws-hid", "ws-us")
    assert step.options.scan_flags == ScanFlags(lineage=True)
    assert step.options.admin_profile == "adm"
    assert step.title == "scan of 3 workspaces (lineage)"
    assert compiled.notes == [] and compiled.unmatched == []


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("sales", ["ws-sales"]),  # case does not matter
        ("FINANCE E?", ["ws-eu"]),
        ("Finance ??", ["ws-eu", "ws-us"]),
        ("*", ["ws-eu", "ws-hid", "ws-sales", "ws-us"]),  # not personal, not deleted
        ("*ale*", ["ws-sales"]),
        ("Finance Personal", []),
        ("Finance Old", []),
        ("Finance", []),  # the whole name has to match
        ("Fin.*", []),  # only * and ? are special
    ],
)
def test_a_name_is_a_pattern_over_active_workspaces_that_are_not_personal(
    pattern, expected
):
    compiled = steps_for(f"  - {{name: '{pattern}', scan: true}}\n")

    found = [i for s in compiled.steps for i in s.options.workspace_ids]
    assert found == expected


def test_the_name_that_comes_with_an_id_is_not_looked_up():
    compiled = steps_for(
        "  - {id: ws-eu, name: Something else entirely, scan: true}\n"
        "  - {id: ws-new, name: Not in the lake yet, details: [pages]}\n"
    )

    scans = [s for s in compiled.steps if s.options.targets == ("scan",)]
    assert [s.options.workspace_ids for s in scans] == [("ws-eu",)]  # by its id
    assert compiled.unmatched == []
    # the label tells the workspace in a message, though the lake does not know it
    assert any(
        "no user account of the plan lists Not in the lake yet (ana, bob)" in note
        for note in compiled.notes
    ), compiled.notes


def test_an_id_is_taken_as_it_is_even_when_the_lake_does_not_know_it():
    compiled = steps_for(
        "  - {id: ws-pers, scan: true}\n  - {id: ws-new, scan: true}\n"
    )

    (step,) = compiled.steps
    assert step.options.workspace_ids == ("ws-new", "ws-pers")


def test_a_name_that_matches_nothing_is_said():
    compiled = steps_for("  - {name: Nothing*, scan: true}\n")

    assert compiled.steps == []
    assert compiled.unmatched == [
        ("workspaces[0] (Nothing*)", "no workspace is called 'Nothing*'")
    ]


def test_a_name_that_matches_nothing_in_an_empty_lake_says_why():
    compiled = steps_for("  - {name: Nothing*, scan: true}\n", lake=[])

    assert "the lake holds no list of workspaces yet" in compiled.unmatched[0][1]


def test_workspaces_that_are_scanned_alike_are_scanned_together():
    compiled = steps_for(
        "  - {name: 'Finance E*', scan: {lineage: true}}\n"
        "  - {id: ws-sales, scan: {lineage: true}}\n"
        "  - {name: 'Finance U*', scan: true}\n"
    )

    assert [
        (s.options.scan_flags, s.options.workspace_ids) for s in compiled.steps
    ] == [
        (ScanFlags(), ("ws-us",)),  # the plainer scan first
        (ScanFlags(lineage=True), ("ws-eu", "ws-sales")),
    ]


def test_the_users_of_every_kind_of_item_are_fetched_by_the_administrator():
    compiled = steps_for("  - {name: Sales, details: [users], via: admin}\n")

    (step,) = compiled.steps
    assert names(step) == [
        "dashboard-users",
        "dataflow-users",
        "dataset-users",
        "group-users",
        "report-users",
    ]
    assert step.options.workspace_ids == ("ws-sales",)
    assert step.options.admin_profile == "adm" and step.options.user_profile is None
    assert step.title == (
        "dashboard-users, dataflow-users, dataset-users, group-users, report-users "
        "for 1 workspace (adm)"
    )


def test_data_sources_and_refreshes_have_their_own_targets():
    (step,) = steps_for(
        "  - {name: Sales, details: [datasources, refreshes], via: admin}\n"
    ).steps

    assert names(step) == ["dataflow-datasources", "datasources", "refreshables"]


def test_what_only_a_user_can_read_is_fetched_by_the_user_that_lists_the_workspace():
    compiled = steps_for(
        "  - {name: 'Finance E*', details: [pages, tiles, parameters], via: auto}\n"
        "  - {name: 'Finance U*', details: [pages], via: auto}\n"
    )

    assert [(names(s), s.options.user_profile) for s in compiled.steps] == [
        (["user-dashboard-tiles", "user-dataset-parameters", "user-pages"], "ana"),
        (["user-pages"], "bob"),
    ]
    assert all(s.options.admin_profile is None for s in compiled.steps)
    assert compiled.steps[1].title == "user-pages for 1 workspace (bob)"


def test_the_first_user_account_that_lists_the_workspace_reads_it():
    (step,) = steps_for("  - {name: Sales, details: [pages]}\n").steps

    assert step.options.user_profile == "ana"  # both list it: the first of the plan


def test_a_workspace_no_user_account_lists_gets_a_note_and_nothing_else():
    compiled = steps_for("  - {name: 'Finance H*', details: [pages]}\n")

    assert compiled.steps == []
    assert compiled.notes == [
        "workspaces[0] (Finance H*): no user account of the plan lists Finance Hidden "
        "(ana, bob), so what only a user can read is not fetched for it; give that "
        "account access to the workspace, or name another with via: <profile>"
    ]


def test_a_workspace_is_not_called_unlisted_while_an_account_has_not_been_asked():
    entry = "  - {name: 'Finance H*', details: [pages]}\n"

    nobody_asked = steps_for(entry, listed=set())
    bob_not_asked = steps_for(entry, listed={"ana"})
    both_asked = steps_for(entry, listed={"ana", "bob"})
    unknown = steps_for(
        entry
    )  # nothing is said about the lists: they are taken as held

    assert nobody_asked.steps == []
    assert nobody_asked.notes == [
        "workspaces[0] (Finance H*): which user account lists Finance Hidden is not "
        "known yet: the lake holds no list of workspaces for ana, bob. A step of the plan "
        "fetches it, and a run works the rest out from it; until then what only a user "
        "can read is not planned for it"
    ]
    # only the account that was not asked is named
    assert "the lake holds no list of workspaces for bob. A step" in (
        bob_not_asked.notes[0]
    )
    for answered in (both_asked, unknown):
        assert "no user account of the plan lists Finance Hidden (ana, bob)" in (
            answered.notes[0]
        )


def test_the_note_about_workspaces_nobody_lists_names_a_few_of_them():
    lake = [workspace(f"W{n}", f"ws-{n}") for n in range(6)]

    note = steps_for("  - {name: 'W*', details: [pages]}\n", lake=lake).notes[0]

    assert "W0, W1, W2 and 3 more" in note


def test_a_profile_that_is_named_reads_whatever_the_lists_say():
    (step,) = steps_for("  - {name: 'Finance H*', details: [pages], via: bob}\n").steps

    assert step.options.user_profile == "bob"
    assert step.options.workspace_ids == ("ws-hid",)


def test_a_user_account_can_be_asked_for_even_where_an_administrator_could_read():
    compiled = steps_for("  - {name: 'Finance E*', details: [users], via: user}\n")

    (step,) = compiled.steps
    assert names(step) == ["user-dataset-users", "user-group-users"]
    assert step.options.user_profile == "ana" and step.options.admin_profile is None
    assert compiled.notes == [
        "workspaces[0] (Finance E*): the users of dashboard, dataflow, report are not "
        "fetched: they need an administrator's account (a user's API has no operation "
        "for them)"
    ]


def test_without_an_administrator_a_user_gives_what_a_user_can():
    compiled = steps_for(
        "  - {name: 'Finance E*', details: [users]}\n", accounting=USER_ONLY
    )

    (step,) = compiled.steps
    assert names(step) == ["user-dataset-users", "user-group-users"]
    assert (
        "the users of dashboard, dataflow, report are not fetched" in compiled.notes[0]
    )


def test_auto_goes_to_the_administrator_for_what_the_administrator_can_read():
    compiled = steps_for(
        "  - {name: Sales, details: [users, datasources, refreshes]}\n"
    )

    (step,) = compiled.steps
    assert step.options.admin_profile == "adm" and step.options.user_profile is None
    assert names(step) == [
        "dashboard-users",
        "dataflow-datasources",
        "dataflow-users",
        "dataset-users",
        "datasources",
        "group-users",
        "refreshables",
        "report-users",
    ]
    assert compiled.notes == []


def test_the_administrator_is_never_given_what_only_a_user_can_read():
    from pbi_cli.core.sync.targets import get_target

    plan = workspaces_file("  - {id: a, details: [users], via: admin}\n")
    entry = plan.workspaces[0]

    picked = plan._pick(entry, _Stand("a"), [get_target("user-pages")], BOTH)

    assert picked is None


def test_what_one_workspace_needs_from_each_entry_is_put_together():
    compiled = steps_for(
        "  - {id: ws-sales, details: [users], via: admin}\n"
        "  - {id: ws-sales, details: [datasources], via: admin}\n"
    )

    (step,) = compiled.steps
    assert (
        "group-users" in step.options.targets and "datasources" in step.options.targets
    )


def test_workspaces_that_need_the_same_are_fetched_in_one_step_and_others_apart():
    compiled = steps_for(
        "  - {id: ws-eu, details: [users], via: admin}\n"
        "  - {id: ws-us, details: [users], via: admin}\n"
        "  - {id: ws-sales, details: [datasources], via: admin}\n"
    )

    assert [
        (len(s.options.targets), s.options.workspace_ids) for s in compiled.steps
    ] == [
        (5, ("ws-eu", "ws-us")),
        (2, ("ws-sales",)),
    ]


def test_steps_come_scans_first_then_the_administrator_then_each_user():
    compiled = steps_for(
        "  - {id: ws-eu, details: [pages], via: ana}\n"
        "  - {id: ws-eu, details: [users], via: admin}\n"
        "  - {id: ws-eu, scan: true}\n"
    )

    kinds = [
        (
            "scan"
            if s.options.targets == ("scan",)
            else "admin" if s.options.admin_profile else "user"
        )
        for s in compiled.steps
    ]
    assert kinds == ["scan", "admin", "user"]


def test_the_steps_of_the_users_come_in_the_order_the_file_lists_them():
    entries = (
        "  - {name: 'Finance E*', details: [pages]}\n"
        "  - {name: 'Finance U*', details: [pages]}\n"
    )

    bob_first = steps_for(entries, accounting=Accounting(users=("bob", "ana")))
    ana_first = steps_for(entries, accounting=Accounting(users=("ana", "bob")))

    assert [
        (s.options.user_profile, s.options.workspace_ids) for s in bob_first.steps
    ] == [("bob", ("ws-us",)), ("ana", ("ws-eu",))]
    assert [s.options.user_profile for s in ana_first.steps] == ["ana", "bob"]


# -- what can never be carried out ---------------------------------------------------------


def test_a_scan_needs_an_administrator():
    with pytest.raises(PlanFileError) as error:
        steps_for("  - {id: a, scan: true}\n", accounting=USER_ONLY)

    assert (
        "pbi-plan.yaml:3: workspaces[0] (a).scan: a scan needs an administrator"
        in str(error.value)
    )


@pytest.mark.parametrize(
    "entry, accounting, expected",
    [
        (
            "details: [pages], via: admin",
            BOTH,
            "'pages' cannot be fetched with the administrator's account: only a "
            "user's account has an operation for it. Use via: auto, via: user, or a "
            "profile name.",
        ),
        (
            "details: [users], via: admin",
            USER_ONLY,
            "'users' needs an administrator account, and none is stored.",
        ),
        (
            "details: [pages], via: user",
            Accounting(admin="adm"),
            "'pages' needs a user account, and none is stored.",
        ),
        (
            "details: [pages], via: ana",
            BOTH,
            None,  # a profile that is named is a user account
        ),
        (
            "details: [pages]",
            ADMIN_ONLY,
            "'pages' needs an account that is not stored: a user's",
        ),
    ],
)
def test_a_detail_that_no_account_can_give_is_refused_with_the_reason(
    entry, accounting, expected
):
    if expected is None:
        steps_for(f"  - {{id: a, {entry}}}\n", accounting=accounting)
        return
    with pytest.raises(PlanFileError) as error:
        steps_for(f"  - {{id: a, {entry}}}\n", accounting=accounting)

    assert expected in str(error.value)
    assert "pbi-plan.yaml:3: workspaces[0] (a).details:" in str(error.value)


def test_auto_fetches_what_it_can_and_says_what_no_stored_account_can():
    compiled = steps_for(
        "  - {id: ws-eu, details: [users, pages, tiles], via: auto}\n",
        accounting=ADMIN_ONLY,
    )

    (step,) = compiled.steps
    assert "group-users" in step.options.targets  # the users: the administrator can
    assert compiled.notes == [
        "workspaces[0] (ws-eu): 'pages' needs an account that is not stored: a user's "
        "(`pbi auth -t <token> -g user`). It is not fetched.",
        "workspaces[0] (ws-eu): 'tiles' needs an account that is not stored: a user's "
        "(`pbi auth -t <token> -g user`). It is not fetched.",
    ]  # and nothing more: there is no user account to look at


def test_auto_that_could_fetch_nothing_is_refused_unless_a_scan_is_asked_for_too():
    with pytest.raises(
        PlanFileError, match="'pages' needs an account that is not stored"
    ):
        steps_for("  - {id: a, details: [pages, tiles]}\n", accounting=ADMIN_ONLY)

    compiled = steps_for(
        "  - {id: a, scan: true, details: [pages]}\n", accounting=ADMIN_ONLY
    )

    assert [s.options.targets for s in compiled.steps] == [("scan",)]
    assert len(compiled.notes) == 1 and "'pages' needs an account" in compiled.notes[0]


def test_an_account_that_is_named_is_not_forgiven_what_it_cannot_give():
    with pytest.raises(PlanFileError, match="'pages' cannot be fetched with the admin"):
        steps_for("  - {id: a, details: [users, pages], via: admin}\n")


def test_the_entry_of_a_user_who_asked_for_everything_about_one_workspace():
    plan = workspaces_file(
        "  - id: ws-it\n    name: IT Management\n"
        "    scan:\n      lineage: true\n      datasource_details: true\n"
        "      dataset_schema: true\n      dataset_expressions: true\n"
        "      get_artifact_users: true\n"
        "    details: [users, datasources, pages, refreshes, parameters, tiles]\n"
        "    via: auto\n"
    )
    lake = [workspace("IT Management", "ws-it", visible_to=["ana"])]

    both = plan.workspace_steps(BOTH, lake)
    admin_only = plan.workspace_steps(ADMIN_ONLY, [workspace("IT Management", "ws-it")])

    assert [(names(s), s.options.user_profile) for s in both.steps][0] == (
        ["scan"],
        None,
    )
    assert both.steps[0].options.scan_flags == ALL_FIVE
    assert any(
        s.options.user_profile == "ana"
        and names(s)
        == ["user-dashboard-tiles", "user-dataset-parameters", "user-pages"]
        for s in both.steps
    )
    assert both.notes == []
    assert [names(s) for s in admin_only.steps][0] == ["scan"]
    assert len(admin_only.notes) == 3  # pages, parameters and tiles: no user account


def test_a_user_can_read_the_refresh_history_of_a_dataset_so_it_is_not_refused():
    (step,) = steps_for("  - {id: ws-eu, details: [refreshes], via: user}\n").steps

    assert names(step) == ["user-dataset-refreshes"]


def test_what_only_the_user_api_has_is_refused_when_the_administrator_is_asked_for():
    with pytest.raises(PlanFileError) as error:
        steps_for(
            "  - {id: a, details: [users], via: ana}\n"
            "  - {id: b, details: [tiles], via: admin}\n"
        )

    assert "workspaces[1] (b).details: 'tiles' cannot be fetched with the " in str(
        error.value
    )


# ---------------------------------------------------------------------------
# what a flag of the command line changes
# ---------------------------------------------------------------------------


def test_the_accounts_are_told_in_words_the_administrator_first():
    assert Accounting(admin="adm", users=("ana", "bob")).labels() == [
        "adm (admin)",
        "ana (user)",
        "bob (user)",
    ]
    assert Accounting(users=("ana",)).labels() == ["ana (user)"]
    assert Accounting().labels() == []


def test_the_scan_settings_are_put_over_the_options_of_a_step():
    options = SyncOptions(targets=("scan",), scan_interval=5.0, scan_timeout=600.0)

    changed = Overrides(scan_interval=1.5, scan_timeout=30.0).apply(options)

    assert (changed.scan_interval, changed.scan_timeout) == (1.5, 30.0)
    assert Overrides(scan_timeout=30.0).apply(options).scan_interval == 5.0
    assert Overrides(scan_interval=1.5).apply(options).scan_timeout == 600.0


def test_the_flags_are_put_over_the_options_of_a_step():
    options = SyncOptions(targets=("groups",), days=7, workers=2, max_wait=5.0)

    changed = Overrides(
        force=True,
        max_age=timedelta(hours=2),
        days=3,
        workers=9,
        wait=30.0,
    ).apply(options)

    assert changed.force and changed.max_age == timedelta(hours=2)
    assert (changed.days, changed.workers, changed.max_wait) == (3, 9, 30.0)
    assert changed.targets == ("groups",)


def test_a_flag_that_is_not_given_changes_nothing():
    options = SyncOptions(targets=("groups",), days=7, workers=2, max_wait=5.0)

    assert Overrides().apply(options) is options
    assert Overrides(wait=0.0).apply(options).max_wait == 0.0  # zero is a value
    assert Overrides(force=False, days=None).apply(options) == options


# ---------------------------------------------------------------------------
# the options of a scan: a mapping, a list of names, or true; and a scan under tenant
# ---------------------------------------------------------------------------

ALL_FIVE = ScanFlags(True, True, True, True, True)


@pytest.mark.parametrize(
    "written",
    [
        "{lineage: true, datasource_details: true, dataset_schema: true, "
        "dataset_expressions: true, get_artifact_users: true}",
        "[lineage, datasource_details, dataset_schema, dataset_expressions, "
        "get_artifact_users]",
    ],
)
def test_the_options_of_a_scan_are_a_mapping_or_a_list_of_names(written):
    tenant = read(f"version: 1\ntenant: {{scan: {written}}}\n").tenant
    entry = read(
        f"version: 1\nworkspaces:\n  - {{id: a, scan: {written}}}\n"
    ).workspaces[0]

    assert tenant.scan == ALL_FIVE and entry.scan == ALL_FIVE


def test_a_list_names_only_the_options_that_are_on():
    plan = read("version: 1\ntenant: {scan: [lineage, get_artifact_users]}\n")

    assert plan.tenant.scan == ScanFlags(lineage=True, get_artifact_users=True)


def test_an_empty_list_is_a_scan_with_no_options():
    plan = read("version: 1\ntenant: {scan: []}\nworkspaces:\n  - {id: a, scan: []}\n")

    assert plan.tenant.scan == ScanFlags() and plan.workspaces[0].scan == ScanFlags()
    assert plan.tenant.targets == ("default", "scan")  # it is a scan


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "version: 1\ntenant:\n  scan:\n    - lineage\n    - dataset_expression\n",
            "pbi-plan.yaml:5: tenant.scan[1]: 'dataset_expression' is not a scan option. "
            "Did you mean 'dataset_expressions'? The options are: lineage, "
            "datasource_details, dataset_schema, dataset_expressions, get_artifact_users.",
        ),
        (
            "version: 1\nworkspaces:\n  - id: a\n    scan: [lineage, nope]\n",
            "pbi-plan.yaml:4: workspaces[0].scan[1]: 'nope' is not a scan option.",
        ),
        (
            "version: 1\ntenant: {scan: [lineage, 3]}\n",
            "pbi-plan.yaml:2: tenant.scan[1]: '3' is not a scan option.",
        ),
    ],
)
def test_a_name_that_is_not_an_option_of_a_scan_says_which_and_what_was_meant(
    text, expected
):
    assert expected in refused(text)


@pytest.mark.parametrize(
    "written", ["{lineage: true}", "[lineage]", "true"], ids=["mapping", "list", "true"]
)
def test_a_scan_under_tenant_asks_for_the_scan_without_naming_it_in_the_targets(
    written,
):
    tenant = read(f"version: 1\ntenant: {{scan: {written}}}\n").tenant

    assert tenant.targets == ("default", "scan")  # the plain sync, and the scan


def test_the_scan_that_a_section_asks_for_is_added_to_the_targets_that_are_named():
    named = read("version: 1\ntenant:\n  targets: [groups]\n  scan: [lineage]\n")
    twice = read("version: 1\ntenant:\n  targets: [scan]\n  scan: [lineage]\n")
    everything = read("version: 1\ntenant:\n  targets: [all]\n  scan: [lineage]\n")

    assert named.tenant.targets == ("groups", "scan")
    assert twice.tenant.targets == ("scan",)  # not named twice
    assert everything.tenant.targets == ("all",)  # all has it already
    assert everything.tenant.scan == ScanFlags(lineage=True)


def test_scan_false_is_no_scan_and_the_target_still_scans_with_no_options():
    off = read("version: 1\ntenant:\n  targets: [groups]\n  scan: false\n").tenant
    named = read("version: 1\ntenant:\n  targets: [scan]\n  scan: false\n").tenant

    assert off.targets == ("groups",) and off.scan == ScanFlags()
    assert named.targets == ("scan",)


def test_the_options_that_only_a_scan_has_need_one_and_say_how_to_get_it():
    for key in ("full_scan", "exclude_personal", "exclude_inactive"):
        message = refused(f"version: 1\ntenant:\n  {key}: true\n")
        assert (
            f"tenant.{key}: has no effect: 'scan' is not among tenant.targets."
            in message
        )
        assert (
            "Add a `scan:` section to tenant (or `scan` to tenant.targets)" in message
        )
        assert read(f"version: 1\ntenant:\n  {key}: true\n  scan: [lineage]\n").tenant
        assert read(f"version: 1\ntenant:\n  targets: [scan]\n  {key}: true\n").tenant


def test_the_days_of_events_need_the_events_and_the_message_says_how_to_get_them():
    message = refused("version: 1\ntenant:\n  activity_days: 7\n")

    assert (
        "tenant.activity_days: has no effect: 'activity' is not among tenant.targets."
        in (message)
    )
    assert (
        "Add `activity` to tenant.targets" in message and "e-mail addresses" in message
    )


USER_FILE = """\
version: 1
tenant:
  targets: [default, activity]
  activity_days: 28 # days of audit events (1 to 28); only with `activity` above
  scan:
    - lineage
    - datasource_details
    - dataset_schema
    - dataset_expressions
    - get_artifact_users
"""


def test_a_tenant_that_keeps_the_events_and_scans_every_workspace_with_all_the_options():
    plan = read(USER_FILE)

    (step,) = plan.first_steps(ADMIN_ONLY)

    assert plan.tenant.targets == ("default", "activity", "scan")
    assert {"groups", "activity", "scan"} <= set(step.options.targets)
    assert step.options.days == 28 and step.options.scan_flags == ALL_FIVE
    assert step.options.admin_profile == "adm"


# ---------------------------------------------------------------------------
# the workspace a session opens
# ---------------------------------------------------------------------------


def test_the_workspace_to_open_is_the_one_with_the_id_else_the_first_that_matches():
    found = find_workspace
    lake = [
        workspace("Finance EU", "ws-eu"),
        workspace("Finance US", "ws-us"),
        workspace("Sales", "ws-sales"),
    ]

    assert found("ws-us", lake).name == "Finance US"
    assert found("Finance*", lake).id == "ws-eu"  # the first in the order of the lake
    assert found("sales", lake).id == "ws-sales"  # case does not matter
    assert found("Finance ?S", lake).id == "ws-us"
    assert found("Nothing", lake) is None
    assert found("Finance", lake) is None  # the whole name has to match


def test_an_id_wins_over_a_name_and_an_active_workspace_over_a_deleted_one():
    found = find_workspace
    lake = [
        workspace("Old", "ws-old", state="Deleted"),
        workspace("ws-new", "ws-1"),  # a workspace whose name looks like an id
        workspace("Old", "ws-new"),
    ]

    assert found("ws-new", lake).id == "ws-new"  # the id, not the name
    assert found("Old", lake).id == "ws-new"  # the active one, not the deleted one
    assert (
        found("Old", [lake[0]]).id == "ws-old"
    )  # but a deleted one if it is all there is


def test_a_personal_workspace_can_be_opened_by_name():
    lake = [workspace("My workspace", "ws-p", kind="PersonalGroup")]

    assert find_workspace("My*", lake).id == "ws-p"


# ---------------------------------------------------------------------------
# the example file
# ---------------------------------------------------------------------------

EXAMPLE_FILE = (
    Path(__file__).resolve().parents[1] / "examples" / "pbi-plan.example.yaml"
)


def test_the_example_file_is_a_plan_that_uses_every_section_and_compiles():
    plan = PlanFile.load(EXAMPLE_FILE)

    assert plan.accounts.admin == "admin-nlm"
    assert plan.accounts.user == ("svc-finance", "svc-sales")
    assert plan.tenant is not None and plan.tenant.activity_days == 7
    assert [w.via for w in plan.workspaces] == ["auto", "svc-finance"]
    assert plan.session.lake and plan.session.open and plan.session.lazy == "ask"

    accounting = plan.accounting(
        Stored(admin=("admin-nlm",), users=("svc-finance", "svc-sales"))
    )
    first = plan.first_steps(accounting)
    later = plan.workspace_steps(
        accounting,
        [
            workspace("Finance EU", "ws-eu"),
            workspace("Finance US", "ws-us"),
        ],
    )

    assert [s.title for s in first] == ["the tenant (admin-nlm)"]
    assert later.unmatched == []
    assert [s.options.targets for s in later.steps][0] == ("scan",)
    assert {s.options.user_profile for s in later.steps} >= {"svc-finance"}
