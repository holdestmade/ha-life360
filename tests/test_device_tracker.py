"""Test Life360 device_tracker.py module."""
from __future__ import annotations

import pytest

from custom_components.life360.device_tracker import Life360DeviceTracker

# Properties HA has deprecated on device tracker entities. Overriding any of them
# causes HA to log a warning at import time, and they will stop working entirely
# in HA 2027.7. Member data is exposed via extra state attributes instead.
DEPRECATED_PROPERTIES = ("battery_level", "location_name")


@pytest.mark.parametrize("name", DEPRECATED_PROPERTIES)
def test_deprecated_properties_not_overridden(name: str) -> None:
    """Test deprecated device tracker properties are not overridden."""
    for cls in Life360DeviceTracker.__mro__:
        if not cls.__module__.startswith("custom_components."):
            continue
        assert name not in vars(cls), f"{cls.__name__} overrides {name}"
