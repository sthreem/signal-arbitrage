"""Getting a call in front of a person without asking them to run anything.

Email is the delivery a customer would eventually want, and it needs credentials
in the Keychain and an address to send to. A desktop notification needs neither
and works today, so it is what the scheduled run uses. Nothing here ever raises:
a delivery failure must not take down the cycle that produced the call.
"""

from __future__ import annotations

import shutil
import subprocess


def desktop(title: str, message: str) -> bool:
    """Raise a macOS notification. False if it could not be delivered."""
    if shutil.which("osascript") is None:
        return False
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    try:
        result = subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_message}" with title "{safe_title}"'],
            capture_output=True, check=False, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
