"""Local entitlement for the final free edition.

Historical license files and registry records are deliberately neither read nor
rewritten. EULA acceptance and content-processing permission remain independent.
"""


class LicenseManager:
    """Compatibility facade: every installation has Pro, including offline."""

    def __init__(self, trial_limit: float = 604800.0, logger=None) -> None:
        pass

    @property
    def is_pro(self) -> bool:
        return True

    @property
    def trial_remaining_seconds(self) -> float:
        return 0.0

    @property
    def trial_expired(self) -> bool:
        return False

    def can_start_recording(self) -> bool:
        return True

    def record_usage(self, seconds: float) -> None:
        pass

    def mark_trial_started(self) -> None:
        pass

    async def activate(self, key: str) -> dict:
        return {"success": True, **self.status()}

    async def deactivate(self) -> dict:
        return {"success": True, **self.status()}

    async def validate(self) -> bool:
        return True

    async def maybe_revalidate(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "is_pro": True,
            "trial_remaining_seconds": 0.0,
            "customer_email": "",
            "customer_name": "",
            "license_key": "",
            "activation_id": "",
            "edition": "final_free",
        }
