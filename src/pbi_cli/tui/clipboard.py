"""Put text on the clipboard of the system, with the tool that the platform has.

A terminal can be asked to set the clipboard (Textual sends the sequence for it), but only some
terminals do as they are asked, and a long text may be cut. So the tool of the system comes
first: ``pbcopy`` on macOS, ``Set-Clipboard`` of PowerShell on Windows, ``wl-copy``, ``xclip`` or
``xsel`` on Linux. This module imports nothing of Textual.

```python
from pbi_cli.tui.clipboard import system_copy

tool = system_copy("a\\tb\\n1\\t2\\n")  # "pbcopy", "powershell", ... or "" when none did
```
"""

import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

#: Seconds a tool gets to take the text.
TIMEOUT = 20.0

#: Read the text from a file as UTF-8, whatever the code page of the console is.
_POWERSHELL = "Get-Content -Raw -Encoding UTF8 -LiteralPath $env:PBI_CLIPBOARD_FILE | Set-Clipboard"


def _run(command: List[str], text: bytes, env: Optional[dict] = None) -> bool:
    """Run a tool that takes the text on its standard input; whether it took it."""
    try:
        subprocess.run(
            command,
            input=text,
            check=True,
            timeout=TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _piped(name: str, *arguments: str, text: str) -> str:
    """The name of the tool when it is there and took the text, else nothing."""
    path = shutil.which(name)
    if path is None:
        return ""
    # a tool that reads the locale (pbcopy) must read the text as the UTF-8 it is
    env = {**os.environ, "LC_CTYPE": "UTF-8"}
    return name if _run([path, *arguments], text.encode("utf-8"), env) else ""


def _powershell(text: str) -> str:
    """Windows: ``Set-Clipboard`` reads the text from a file, so that no code page of the
    console and no quoting gets in the way of a letter."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return ""
    # the people and addresses in it go through a file for a moment: nobody else may read it
    handle, name = tempfile.mkstemp(prefix="pbi-clipboard-", suffix=".txt")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))
        env = {**os.environ, "PBI_CLIPBOARD_FILE": name}
        command = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-Command",
            _POWERSHELL,
        ]
        return "powershell" if _run(command, b"", env) else ""
    finally:
        try:
            os.remove(name)
        except OSError:
            pass


def system_copy(text: str) -> str:
    """Put text on the clipboard of the system.

    :return: the name of the tool that did it, or nothing when there is none (or it failed):
        the caller then relies on the terminal
    """
    if sys.platform == "darwin":
        return _piped("pbcopy", text=text)
    if sys.platform in ("win32", "cygwin"):
        return _powershell(text)
    if os.environ.get("WAYLAND_DISPLAY"):
        found = _piped("wl-copy", text=text)
        if found:
            return found
    return _piped("xclip", "-selection", "clipboard", text=text) or _piped(
        "xsel", "--clipboard", "--input", text=text
    )
