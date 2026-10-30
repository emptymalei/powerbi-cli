"""Get a bearer token by signing in with PowerShell, the way ``Login-PowerBI`` does.

The Power BI module for PowerShell (``MicrosoftPowerBIMgmt``) opens the Microsoft sign-in
window with its own client id, and ``Get-PowerBIAccessToken`` gives the token. Nothing is
stored here: the caller stores the token for a profile and a group.

```python
from pbi_cli.core.powershell import powershell_token

token = powershell_token()
```
"""

import shutil
import subprocess
from typing import List, Optional

from pbi_cli.errors import PBIError

#: Seconds the person gets to finish the sign-in window.
LOGIN_TIMEOUT = 300.0

SCRIPT = "Login-PowerBI | Out-Null; Get-PowerBIAccessToken -AsString"

HINT = "Install the module once with: Install-Module MicrosoftPowerBIMgmt -Scope CurrentUser"


def _shell() -> Optional[str]:
    return shutil.which("powershell") or shutil.which("pwsh")


def parse_token(output: str) -> str:
    """The token in what ``Get-PowerBIAccessToken -AsString`` printed: its last line, without
    the ``Bearer`` prefix."""
    lines: List[str] = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    token = lines[-1]
    if token.lower().startswith("bearer"):
        token = token[len("bearer") :]
    return "".join(token.split())


def powershell_token(timeout: float = LOGIN_TIMEOUT) -> str:
    """Sign in with PowerShell (a window opens) and return the bearer token.

    :raises PBIError: when PowerShell or its Power BI module is missing, the sign-in fails
        or is not finished in time
    """
    shell = _shell()
    if shell is None:
        raise PBIError(f"PowerShell was not found. {HINT}")
    try:
        done = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PBIError("The PowerShell sign-in was not finished in time.") from error
    except OSError as error:
        raise PBIError(f"PowerShell could not be started: {error}") from error
    token = parse_token(done.stdout) if done.returncode == 0 else ""
    if not token:
        reason = (done.stderr or "").strip().splitlines()
        detail = f" ({reason[-1]})" if reason else ""
        raise PBIError(f"The PowerShell sign-in gave no token{detail}. {HINT}")
    return token
