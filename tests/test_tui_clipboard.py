"""Putting text on the clipboard of the system: which tool, and what is passed to it."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from pbi_cli.tui import clipboard

#: what the tests of this module call: the autouse fixture of conftest replaces the function
#: that the app calls, so that no test touches the clipboard of the person who runs them
real_system_copy = clipboard.system_copy


class Machine:
    """A computer that has some tools, and a ``subprocess.run`` that records what it is given."""

    def __init__(self, monkeypatch, platform, tools=(), fails=(), timeout=()):
        self.calls = []
        self.files = {}
        self.fails, self.timeout = set(fails), set(timeout)
        self.tools = {name: f"/bin/{name}" for name in tools}
        monkeypatch.setattr(clipboard, "sys", SimpleNamespace(platform=platform))
        monkeypatch.setattr(
            clipboard.shutil, "which", lambda name: self.tools.get(name)
        )
        monkeypatch.setattr(clipboard.subprocess, "run", self.run)

    def run(self, command, **kwargs):
        name = os.path.basename(command[0])
        env = kwargs.get("env") or {}
        path = env.get("PBI_CLIPBOARD_FILE")
        if path:  # what PowerShell would read, while the file is there
            with open(path, "rb") as stream:
                self.files[path] = stream.read()
        self.calls.append((command, kwargs))
        if name in self.timeout:
            raise subprocess.TimeoutExpired(command, 1)
        if name in self.fails:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)


def test_macos_pipes_the_text_to_pbcopy_as_utf8(monkeypatch):
    machine = Machine(monkeypatch, "darwin", tools=["pbcopy"])

    assert real_system_copy("Ann Ås\tx\n") == "pbcopy"

    ((command, kwargs),) = machine.calls
    assert command == ["/bin/pbcopy"]
    assert kwargs["input"] == "Ann Ås\tx\n".encode("utf-8")
    assert kwargs["env"]["LC_CTYPE"] == "UTF-8"
    assert kwargs["timeout"] == clipboard.TIMEOUT and kwargs["check"] is True


def test_a_tool_that_is_not_there_gives_nothing(monkeypatch):
    machine = Machine(monkeypatch, "darwin", tools=[])

    assert real_system_copy("x") == "" and machine.calls == []


@pytest.mark.parametrize("how", ["fails", "timeout"])
def test_a_tool_that_fails_or_takes_too_long_gives_nothing(monkeypatch, how):
    Machine(monkeypatch, "darwin", tools=["pbcopy"], **{how: ["pbcopy"]})

    assert real_system_copy("x") == ""


def test_linux_tries_wayland_first_when_there_is_a_wayland_session(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    machine = Machine(monkeypatch, "linux", tools=["wl-copy", "xclip", "xsel"])

    assert real_system_copy("x") == "wl-copy"
    assert [os.path.basename(c[0][0]) for c in machine.calls] == ["wl-copy"]


def test_linux_without_a_wayland_session_uses_xclip_for_the_clipboard_selection(
    monkeypatch,
):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    machine = Machine(monkeypatch, "linux", tools=["wl-copy", "xclip", "xsel"])

    assert real_system_copy("x") == "xclip"
    ((command, _),) = machine.calls
    assert command == ["/bin/xclip", "-selection", "clipboard"]


def test_linux_goes_on_to_the_next_tool_when_one_fails(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    machine = Machine(
        monkeypatch,
        "linux",
        tools=["wl-copy", "xclip", "xsel"],
        fails=["wl-copy", "xclip"],
    )

    assert real_system_copy("x") == "xsel"
    assert [c[0] for c in machine.calls][-1] == ["/bin/xsel", "--clipboard", "--input"]


def test_linux_with_no_tool_gives_nothing(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    Machine(monkeypatch, "linux", tools=[])

    assert real_system_copy("x") == ""


def test_windows_has_powershell_read_the_text_from_a_file_that_is_removed_after(
    monkeypatch,
):
    machine = Machine(monkeypatch, "win32", tools=["powershell"])

    assert real_system_copy("Ann Ås\nBob") == "powershell"

    ((command, kwargs),) = machine.calls
    assert command[:5] == [
        "/bin/powershell",
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-Command",
    ]
    assert "Set-Clipboard" in command[5] and "UTF8" in command[5]
    path = kwargs["env"]["PBI_CLIPBOARD_FILE"]
    assert machine.files[path] == "Ann Ås\r\nBob".encode(
        "utf-8"
    )  # lines end as Windows has them
    assert not os.path.exists(path)


def test_windows_uses_pwsh_when_there_is_no_windows_powershell(monkeypatch):
    machine = Machine(monkeypatch, "win32", tools=["pwsh"])

    assert real_system_copy("x") == "powershell"
    assert machine.calls[0][0][0] == "/bin/pwsh"


def test_windows_with_no_shell_gives_nothing(monkeypatch):
    machine = Machine(monkeypatch, "win32", tools=[])

    assert real_system_copy("x") == "" and machine.calls == []


def test_the_file_of_a_failed_windows_copy_is_removed_too(monkeypatch):
    machine = Machine(monkeypatch, "win32", tools=["powershell"], fails=["powershell"])

    assert real_system_copy("x") == ""
    (path,) = machine.files
    assert not os.path.exists(path)


@pytest.mark.skipif(os.name == "nt", reason="the mode of a file is not a thing there")
def test_the_file_that_holds_the_text_for_a_moment_is_private(monkeypatch):
    seen = []
    original = clipboard.os.fdopen

    def spy(handle, *args, **kwargs):
        seen.append(os.fstat(handle).st_mode & 0o777)
        return original(handle, *args, **kwargs)

    monkeypatch.setattr(clipboard.os, "fdopen", spy)
    Machine(monkeypatch, "win32", tools=["powershell"])

    real_system_copy("x")

    assert seen == [0o600]
