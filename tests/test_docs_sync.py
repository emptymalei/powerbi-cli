"""docs/sync.md lists the targets of a sync: keep it in step with the catalog."""

import re
from pathlib import Path

from pbi_cli.core.sync.targets import TARGETS

PAGE = Path(__file__).resolve().parents[1] / "docs" / "sync.md"


def table_rows():
    """``{target name: [cells]}`` for the rows of the targets table."""
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## What can be synced", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"\| `([a-z-]+)` \|", line)
        if match:
            rows[match.group(1)] = [
                c.strip() for c in line.strip().strip("|").split("|")
            ]
    return rows


def test_every_target_is_in_the_table_as_the_catalog_has_it():
    rows = table_rows()
    for target in TARGETS:
        assert target.name in rows, f"{target.name} is missing from docs/sync.md"
        _, keeps, operation, needs, plain = rows[target.name]
        assert operation == f"`{target.endpoint}`", target.name
        assert needs == target.scope.value, target.name
        assert plain.startswith("yes" if target.default else "no"), target.name
        assert keeps.lower().startswith(
            target.title.split(",")[0].lower()[:12]
        ), target.name


def test_what_is_not_in_a_plain_sync_says_why():
    rows = table_rows()
    for target in TARGETS:
        if target.sensitive:
            assert target.sensitive in rows[target.name][4], target.name


def test_the_table_lists_no_target_the_catalog_does_not_have():
    assert set(table_rows()) == {target.name for target in TARGETS}


def test_the_quick_start_names_the_commands_that_exist():
    text = PAGE.read_text(encoding="utf-8")
    for command in ("pbi sync plan", "pbi sync run", "pbi sync status"):
        assert command in text
