"""Guard the CLI surface (commands, flags, defaults, help) against accidental change.

The fixtures in ``tests/fixtures`` were recorded from a known-good version of the CLI
(``uv run python tests/cli_surface.py --write``). Existing scripts rely on flags such as
``-ft``, ``-tf``, ``-wn`` and ``-wi``, so a refactor must not change them.

If a difference is intentional, do not just regenerate the fixtures: add the exact key to
``ACCEPTED_SURFACE_CHANGES`` / ``ACCEPTED_HELP_CHANGES`` with the reason, and mention it in
``docs/changelog.md``. Accepted entries that no longer differ fail the test, so the lists
stay honest.
"""

import difflib
import json
from typing import Any, Dict, List, Tuple

import cli_surface as surface
import pytest

# Key format: "<command path>/<field path>", e.g. "pbi workspaces scan get/params/interval/type/name".
# An entry covers every difference at or below that key. Value = reason.
ACCEPTED_SURFACE_CHANGES: Dict[str, str] = {
    # Typer adds shell completion to the root command.
    "pbi/param_order": "root gains --install-completion and --show-completion",
    "pbi/params/install_completion": "new: pbi --install-completion",
    "pbi/params/show_completion": "new: pbi --show-completion",
    # The folder arguments must stay plain strings: a Path would rewrite s3:// URLs.
    "pbi config set-cache-folder/params/folder_path/type": "Typer has no str-returning path type",
    "pbi config set-output-folder/params/folder_path/type": "Typer has no str-returning path type",
    # --top has a default of 1000; it was (wrongly) marked required.
    "pbi workspaces list/params/top/required": "--top has a default, so it was never required",
    # "x > 0" is now checked by a callback: Typer only has inclusive bounds.
    "pbi workspaces scan get/params/interval/type": "value must still be > 0, checked by a callback",
    "pbi workspaces scan get/params/timeout/type": "value must still be > 0, checked by a callback",
    # The data lake: commands that browse and tidy what the other commands fetched.
    "pbi/command_order": "the root gains the lake, sync and tui commands",
    "pbi lake": "new: pbi lake",
    "pbi lake ls": "new: pbi lake ls",
    "pbi lake show": "new: pbi lake show",
    "pbi lake prune": "new: pbi lake prune",
    # pbi sync: keeps the tenant in the data lake
    "pbi sync": "new: pbi sync",
    "pbi sync plan": "new: pbi sync plan",
    "pbi sync run": "new: pbi sync run",
    "pbi sync status": "new: pbi sync status",
    # pbi tui: browse the data lake and sync it in a terminal UI
    "pbi tui": "new: pbi tui",
    # Help texts only (no flag changed): they now describe the data lake.
    "pbi workspaces list/help": "says the answer is stored in the data lake and what --use-cache and --cache-only mean",
    "pbi users user-access/help": "same, and says that it reads every page and needs an admin",
    "pbi apps list/help": "same, and says what --role admin and --role user list",
    "pbi reports list/help": "says the answer is also stored in the data lake",
    "pbi reports pages/help": "says the answers are also stored in the data lake",
    "pbi cache/help": "the cache is now the legacy layout; points to pbi lake",
    "pbi cache list/help": "says it lists the legacy cache only",
    "pbi cache clear/help": "says it never touches the data lake",
    "pbi workspaces scan batch/help": "scans up to 100 workspaces per request, falls back to one by one, stops on credential errors",
}

# Key format: "<command path> --help" or "<command path> (no subcommand)". Value = reason.
ACCEPTED_HELP_CHANGES: Dict[str, str] = {}

REGENERATE_HINT = (
    "If this change is intentional, record it in ACCEPTED_*_CHANGES with a reason "
    "(and in docs/changelog.md). Regenerate the fixtures only for a reviewed, "
    "intentional CLI change: uv run python tests/cli_surface.py --write"
)


@pytest.fixture(scope="module")
def live() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    runner, target, command = surface.load_cli()
    return (
        surface.dump_surface(command),
        surface.capture_help(runner, target, command),
    )


def _normalize_command(info: Dict[str, Any]) -> Dict[str, Any]:
    """Key parameters by name so diffs point at a flag, not at a list index."""
    info = dict(info)
    params: List[Dict[str, Any]] = info.pop("params")
    info["params"] = {p["name"]: p for p in params}
    info["param_order"] = [p["name"] for p in params]
    return info


def _flatten(
    prefix: Tuple[str, ...], value: Any, out: Dict[Tuple[str, ...], Any]
) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            _flatten(prefix + (key,), sub, out)
    else:
        out[prefix] = value


def _surface_differences(
    expected: Dict[str, Any], actual: Dict[str, Any]
) -> Dict[str, str]:
    """Return ``{key: description}`` for every difference between two surfaces."""
    differences: Dict[str, str] = {}
    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            differences[path] = "command is missing"
            continue
        if path not in expected:
            differences[path] = "command was added"
            continue
        flat_expected: Dict[Tuple[str, ...], Any] = {}
        flat_actual: Dict[Tuple[str, ...], Any] = {}
        _flatten((), _normalize_command(expected[path]), flat_expected)
        _flatten((), _normalize_command(actual[path]), flat_actual)
        for field in sorted(set(flat_expected) | set(flat_actual)):
            old = flat_expected.get(field, "<absent>")
            new = flat_actual.get(field, "<absent>")
            if old != new:
                differences[f"{path}/{'/'.join(field)}"] = f"{old!r} -> {new!r}"
    return differences


def _split_accepted(
    differences: Dict[str, str], accepted: Dict[str, str]
) -> Tuple[Dict[str, str], List[str]]:
    """Return ``(unexpected differences, stale accepted keys)``."""

    def is_accepted(key: str) -> bool:
        return any(key == a or key.startswith(a + "/") for a in accepted)

    unexpected = {k: v for k, v in differences.items() if not is_accepted(k)}
    stale = [
        a
        for a in accepted
        if not any(k == a or k.startswith(a + "/") for k in differences)
    ]
    return unexpected, stale


def test_cli_surface_is_unchanged(live):
    expected = json.loads(surface.SURFACE_FILE.read_text(encoding="utf-8"))
    differences = _surface_differences(expected, live[0])
    unexpected, stale = _split_accepted(differences, ACCEPTED_SURFACE_CHANGES)

    report = "\n".join(f"  {key}: {desc}" for key, desc in unexpected.items())
    assert not unexpected, f"CLI surface changed:\n{report}\n{REGENERATE_HINT}"
    assert not stale, f"Accepted surface changes that no longer differ: {stale}"


def test_cli_help_output_is_unchanged(live):
    expected = json.loads(surface.HELP_FILE.read_text(encoding="utf-8"))
    actual = live[1]

    differing = {
        key
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    }
    unexpected = sorted(k for k in differing if k not in ACCEPTED_HELP_CHANGES)
    stale = sorted(k for k in ACCEPTED_HELP_CHANGES if k not in differing)

    if unexpected:
        key = unexpected[0]
        old = (expected.get(key) or {}).get("output", "<absent>").splitlines()
        new = (actual.get(key) or {}).get("output", "<absent>").splitlines()
        diff = "\n".join(
            difflib.unified_diff(old, new, "recorded", "live", lineterm="")
        )
        pytest.fail(
            f"Help/greeting output changed for {len(unexpected)} command(s): {unexpected}\n"
            f"First difference ({key}):\n{diff}\n{REGENERATE_HINT}"
        )
    assert not stale, f"Accepted help changes that no longer differ: {stale}"


def test_surface_helper_detects_a_changed_flag():
    """The comparison itself must notice a renamed flag and a changed default."""
    expected = json.loads(surface.SURFACE_FILE.read_text(encoding="utf-8"))
    mutated = json.loads(json.dumps(expected))
    params = mutated["pbi workspaces scan get"]["params"]
    interval = next(p for p in params if p["name"] == "interval")
    interval["default"] = 99.0
    interval["opts"] = ["--every"]

    differences = _surface_differences(expected, mutated)
    assert "pbi workspaces scan get/params/interval/default" in differences
    assert "pbi workspaces scan get/params/interval/opts" in differences
    unexpected, _ = _split_accepted(differences, {})
    assert unexpected
