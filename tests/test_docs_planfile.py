"""docs/plan-file.md describes the file the code reads: keep the two in step."""

import re
from pathlib import Path

from pbi_cli import cli_sync
from pbi_cli.core import planfile
from pbi_cli.core.details import TITLES
from pbi_cli.core.registry import Scope
from pbi_cli.core.sync.targets import TARGETS

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "plan-file.md"
TEXT = PAGE.read_text(encoding="utf-8")


def details_table():
    """``{(detail, item): (administrator cell, user cell)}`` of the table of details."""
    section = TEXT.split("| Detail | For |", 1)[1].split("\n\n", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"\| `(\w+)` \| (\w+) \| (.*) \| (.*) \|$", line)
        if match:
            detail, item, admin, user = match.groups()
            rows[(detail, item)] = (admin, user)
    return rows


def test_the_example_is_shown_in_the_page_from_the_file_itself():
    assert '--8<-- "examples/pbi-plan.example.yaml"' in TEXT
    assert (ROOT / "examples" / "pbi-plan.example.yaml").exists()


def test_the_table_of_details_says_which_target_fetches_what_for_each_account():
    rows = details_table()
    asked = {(t.detail, t.item): [] for t in TARGETS if t.item and t.detail}
    for target in TARGETS:
        if target.item and target.detail:
            asked[(target.detail, target.item)].append(target)

    assert set(rows) == set(asked)
    for key, targets in asked.items():
        admin_cell, user_cell = rows[key]
        for scope, cell in ((Scope.ADMIN, admin_cell), (Scope.USER, user_cell)):
            names = [t.name for t in targets if t.scope is scope]
            if names:
                assert f"`{names[0]}`" in cell, (key, scope)
            else:
                assert cell == "not possible", (key, scope)


def test_every_detail_is_in_the_table_and_described():
    assert {detail for detail, _ in details_table()} == set(TITLES)
    for detail in TITLES:
        assert f"`{detail}`" in TEXT


def test_every_key_of_the_file_is_documented():
    keys = set(planfile._TOP_KEYS) | set(planfile._ACCOUNT_KEYS)
    keys |= set(planfile._TENANT_KEYS) | set(planfile._WORKSPACE_KEYS)
    keys |= set(planfile._SESSION_KEYS) | set(planfile.SCAN_KEYS)
    for key in keys:
        assert f"`{key}`" in TEXT, f"`{key}` is not described in docs/plan-file.md"


def test_the_words_of_via_and_the_modes_of_lazy_are_documented():
    for word in (planfile.VIA_AUTO, planfile.VIA_ADMIN, planfile.VIA_USER):
        assert f"| `{word}`" in TEXT or f"`{word}` (the default)" in TEXT, word
    for mode in planfile.LAZY_MODES:
        assert f"`{mode}`" in TEXT, mode


def test_the_options_that_go_with_a_plan_file_and_those_that_do_not_are_named():
    for flag in ("--force", "--max-age", "--workers", "--wait", "--days"):
        assert f"`{flag}`" in TEXT, flag
    for flag, _ in cli_sync._IN_THE_FILE.values():
        assert f"`{flag}`" in TEXT or flag in TEXT, flag
    for command in ("pbi sync plan --config", "pbi sync run  --config"):
        assert command in TEXT


def test_the_page_is_in_the_navigation_and_linked_from_where_it_matters():
    assert '"Plan file" = "plan-file.md"' in (ROOT / "zensical.toml").read_text()
    assert "(plan-file.md)" in (ROOT / "docs" / "sync.md").read_text(encoding="utf-8")
    assert "(plan-file.md)" in (ROOT / "docs" / "auth.md").read_text(encoding="utf-8")
    assert "docs/plan-file.md" in (ROOT / "README.md").read_text(encoding="utf-8")
