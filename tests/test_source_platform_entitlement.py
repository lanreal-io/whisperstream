"""The platform port retains unrestricted source use without paid state."""
import pytest

from whisperstream.history_security import PlatformHistorySecurityFactory
from whisperstream.history_security_contract import LicenseState
from whisperstream.license_manager import LicenseManager


@pytest.mark.parametrize("system,machine", [("Darwin", "arm64"), ("Windows", "x86_64")])
def test_platform_entitlement_has_no_paid_cache_or_activation(system, machine):
    manager = LicenseManager()
    factory = PlatformHistorySecurityFactory(
        history_store=lambda: None,
        license_manager=lambda: manager,
        protected_store=lambda: None,
        lockdown_enabled=lambda: False,
        lockdown_locked=lambda: False,
        system=system,
        machine=machine,
    )
    adapter = factory.licensing()
    snapshot = adapter.snapshot()
    assert snapshot.state is LicenseState.PRO
    assert snapshot.cache_integrity_protected is False
    assert snapshot.cache_confidential is False
    assert snapshot.activation_available is False
    assert snapshot.offline_validation_available is False
    assert snapshot.deactivation_available is False
    assert adapter.status()["is_pro"] is True
    assert manager.can_start_recording() is True
    assert manager.trial_expired is False
    assert manager.status()["edition"] == "final_free"


def test_mac_capability_reason_describes_unrestricted_source_use():
    from whisperstream.daily_use_capabilities import (
        CapabilityState,
        create_daily_use_capability_report,
    )
    report = create_daily_use_capability_report("Darwin")
    licensing = next(item for item in report.capabilities if item.capability_id == "licensing")
    assert licensing.state is CapabilityState.AVAILABLE
    assert licensing.reason_code == "unrestricted-source-entitlement"
    assert "trial" not in str(licensing.as_dict())
