"""macOS Keychain integration for vault passphrase auto-unlock.

Lets TARS recover from a restart without an operator at the keyboard.
The passphrase lives in the user's login keychain (AES-encrypted at rest,
unlocked when the user logs in) and is fetched via the `security` CLI.

No-ops on non-macOS platforms — callers should fall through to the next
unlock source.

One-time setup:
    security add-generic-password -s tars-vault -a default -U

After that, `get_passphrase()` returns the stored value as long as the
caller's process runs under a user whose login keychain is unlocked.
"""

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

SERVICE = "tars-vault"
DEFAULT_ACCOUNT = "default"


def is_available() -> bool:
    """True when running on macOS with the `security` CLI present."""
    return sys.platform == "darwin" and shutil.which("security") is not None


def get_passphrase(account: str = DEFAULT_ACCOUNT) -> str | None:
    """Return the vault passphrase from Keychain, or None if unavailable.

    Returns None on non-macOS, missing CLI, locked keychain, or no entry.
    Never raises — callers can chain this with other unlock sources.
    """
    if not is_available():
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"Keychain lookup error: {e}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None
