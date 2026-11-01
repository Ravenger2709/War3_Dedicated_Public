"""Local diagnostics and secret handling; iCCup transport remains unavailable."""

from src.connector import require_supported_connector


def status() -> dict:
    """Deliberately independent of TCP diagnostics or operator-supplied flags."""
    return {
        "state": "BLOCKED",
        "reason": "ICCup_AUTH_BLOCKED",
        "adapter": None,
        "ready": False,
        "iccup_authenticated": False,
        "contract": "docs/contracts/phase3-relay.md",
    }


def start() -> None:
    """Do not read credentials, spawn processes or open sockets before this gate."""
    require_supported_connector("iccup")
