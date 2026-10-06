"""docs/sharing.md describes what `pbi lake publish` does: keep the two in step."""

import re
from pathlib import Path

import typer.main

from pbi_cli.cli import app
from pbi_cli.core.publish import CATEGORIES, RUNS_KEPT
from pbi_cli.session import LAKE_ENV

PAGE = Path(__file__).resolve().parents[1] / "docs" / "sharing.md"
TEXT = PAGE.read_text(encoding="utf-8")


def table_rows():
    """``{category: [cells]}`` for the rows of the table of categories."""
    section = TEXT.split("### What it shows before it copies", 1)[1].split("\n### ", 1)[
        0
    ]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"\| `([a-z]+)` \|", line)
        if match:
            rows[match.group(1)] = [
                c.strip() for c in line.strip().strip("|").split("|")
            ]
    return rows


def test_every_category_is_in_the_table_with_what_it_can_hold():
    rows = table_rows()

    assert set(rows) == {category.name for category in CATEGORIES}
    for category in CATEGORIES:
        _, leave_out, holds = rows[category.name]
        assert holds == category.sensitive, category.name
        expected = (
            f"`--exclude {category.name}`"
            if category.excludable
            else "(cannot be left out)"
        )
        assert leave_out == expected, category.name


def test_the_page_says_how_many_runs_a_published_lake_keeps():
    words = {3: "three", 5: "five", 10: "ten"}

    assert f"the last {words[RUNS_KEPT]} syncs" in TEXT


def test_the_page_names_every_option_of_publish_and_the_environment_variable():
    lake = typer.main.get_command(app).commands["lake"]  # type: ignore[attr-defined]
    publish = lake.commands["publish"]
    options = {
        name
        for parameter in publish.params
        for name in getattr(parameter, "opts", [])
        if name.startswith("--") and name != "--help"
    }

    assert options == {
        "--tenant",
        "--exclude",
        "--history",
        "--prune",
        "--dry-run",
        "--yes",
        "--force",
        "--lake",
    }
    for name in options - {"--tenant"}:
        assert name in TEXT, f"docs/sharing.md never mentions {name}"
    assert LAKE_ENV in TEXT and "publish.json" in TEXT
