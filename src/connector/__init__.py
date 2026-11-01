"""Connection policy; no unverified iCCup authentication implementation."""


class UnsupportedAuthentication(RuntimeError):
    pass


def require_supported_connector(mode: str, adapter: str = None) -> None:
    if mode == "iccup" and adapter == "original-star-v1":
        return
    if mode == "iccup":
        raise UnsupportedAuthentication(
            "ICCup_AUTH_BLOCKED: no verified supported headless adapter is implemented; "
            "see docs/contracts/phase3-runtime.md. No connection was attempted."
        )
    if mode != "offline":
        raise ValueError("mode must be offline or iccup")
