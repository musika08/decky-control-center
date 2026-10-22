"""Runs short Windows PowerShell scripts with a timeout and no window."""
import base64
import os
import subprocess
from typing import Callable, Optional

# Windows PowerShell 5.1: WinRT types (Windows.Devices.Radios) are not loadable from PowerShell 7.
POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")
CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT = 15.0

# (argv, timeout) -> (returncode, stdout, stderr). Tests replace it.
Runner = Callable[[list[str], float], "tuple[int, str, str]"]


class PowerShellError(RuntimeError):
    pass


def subprocess_runner(argv: list[str], timeout: float) -> "tuple[int, str, str]":
    p = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def powershell_argv(script: str) -> list[str]:
    # -EncodedCommand avoids every quoting problem; output is forced to UTF-8 for the decode above.
    full = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$ProgressPreference = 'SilentlyContinue'\n" + script
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def run_powershell(script: str, runner: Optional[Runner] = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return the script's trimmed stdout. Raises PowerShellError on a non-zero exit or a timeout."""
    run = runner or subprocess_runner
    try:
        code, out, err = run(powershell_argv(script), timeout)
    except subprocess.TimeoutExpired as e:
        raise PowerShellError(f"PowerShell timed out after {timeout:g}s") from e
    if code != 0:
        raise PowerShellError((err or out).strip() or f"PowerShell exited with {code}")
    return out.strip()


def decode_script(argv: list[str]) -> str:
    """Inverse of powershell_argv, for tests and logging."""
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
