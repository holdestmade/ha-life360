"""Test Life360 Member sensor & binary sensor entities."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from itertools import chain, repeat
import re
from typing import Any, cast

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN, UPDATE_INTERVAL
from custom_components.life360.helpers import MemberID
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
    async_fire_time_changed,
)

from homeassistant.components.binary_sensor import DOMAIN as BS_DOMAIN
from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.components.sensor import DOMAIN as S_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter

from .common import DtNowMock, assert_log_messages  # noqa: TID251

# Chosen so that converted values are exact: 32.8084 ft == 10 m, 4 mph raw == 9.0 mph.
LAST_SEEN = 1700000000
AT_LOC_SINCE = 1699999000
RAW_SPEED = 4.0
SPEED_MPH = 9.0
GPS_ACCURACY_M = 10.0

cir = {"id": "cid1", "name": "Circle1"}
mem = {
    "id": "mid1",
    "firstName": "First1",
    "lastName": "Last1",
    "avatar": None,
    "features": {"shareLocation": 1},
    "location": {
        "address1": "1 Main St",
        "address2": "",
        "since": AT_LOC_SINCE,
        "isDriving": "0",
        "accuracy": "32.8084",
        "timestamp": LAST_SEEN,
        "latitude": "12.345678",
        "longitude": "-98.765432",
        "name": "Grandma's",
        "speed": RAW_SPEED,
        "battery": "88",
        "charge": "1",
        "wifiState": "1",
    },
    "issues": {"title": "", "dialog": ""},
}


def _entity_id(
    entity_registry: er.EntityRegistry, domain: str, mid: MemberID, key: str
) -> str:
    """Return entity ID of Member's entity for given attribute."""
    entity_id = entity_registry.async_get_entity_id(domain, DOMAIN, f"{mid}_{key}")
    assert entity_id, f"No {domain} entity for {key}"
    return entity_id


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir]),
                "get_circle_members": repeat([mem]),
                "get_circle_member": repeat(mem),
            },
        },
    ],
    indirect=["MockLife360"],
)
async def test_member_sensors(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test a sensor is created for each of the Member's attributes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options={
            "accounts": {
                "aid1": {"authorization": "auth1", "password": None, "enabled": True}
            },
            "driving": False,
            "driving_speed": None,
            "max_gps_accuracy": None,
            "verbosity": 3,
        },
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

    mid = MemberID(cast(str, mem["id"]))
    name = "Life360 First1 Last1"

    expected: dict[str, Any] = {
        "address": "1 Main St",
        "at_loc_since": dt_util.utc_from_timestamp(AT_LOC_SINCE).isoformat(),
        "battery_level": "88",
        "gps_accuracy": str(GPS_ACCURACY_M),
        "ignored_update_reasons": STATE_UNKNOWN,
        "last_seen": dt_util.utc_from_timestamp(LAST_SEEN).isoformat(),
        "latitude": "12.345678",
        "longitude": "-98.765432",
        "place": "Grandma's",
        "reason": STATE_UNKNOWN,
    }
    for key, value in expected.items():
        entity_id = _entity_id(entity_registry, S_DOMAIN, mid, key)
        assert entity_id == f"{S_DOMAIN}.{name.lower().replace(' ', '_')}_{key}"
        state = hass.states.get(entity_id)
        assert state, f"No state for {entity_id}"
        assert state.state == value, f"Unexpected state for {entity_id}"

    # Speed is reported in MPH, and converted by HA per the unit system.
    state = hass.states.get(_entity_id(entity_registry, S_DOMAIN, mid, "speed"))
    assert state
    expected_speed = SpeedConverter.convert(
        SPEED_MPH,
        UnitOfSpeed.MILES_PER_HOUR,
        cast(str, state.attributes["unit_of_measurement"]),
    )
    assert float(state.state) == pytest.approx(expected_speed)

    expected_bs = {
        "battery_charging": STATE_ON,
        "driving": STATE_OFF,
        "wifi_on": STATE_ON,
    }
    for key, value in expected_bs.items():
        entity_id = _entity_id(entity_registry, BS_DOMAIN, mid, key)
        state = hass.states.get(entity_id)
        assert state, f"No state for {entity_id}"
        assert state.state == value, f"Unexpected state for {entity_id}"


# Member data w/ an older last_seen, which the integration ignores.
old_mem = deepcopy(mem)
cast(dict[str, Any], old_mem["location"]).update(
    {
        "address1": "2 Other St",
        "timestamp": LAST_SEEN - 60,
        "speed": RAW_SPEED * 2,
        "battery": "50",
    }
)


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir]),
                "get_circle_members": repeat([mem]),
                "get_circle_member": chain([mem], repeat(old_mem)),
            },
        },
    ],
    indirect=["MockLife360"],
)
async def test_ignored_update(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    dt_now: DtNowMock,
) -> None:
    """Test Member's entities agree when a location update is ignored."""
    dt_now_real, dt_now_mock = dt_now

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options={
            "accounts": {
                "aid1": {"authorization": "auth1", "password": None, "enabled": True}
            },
            "driving": False,
            "driving_speed": None,
            "max_gps_accuracy": None,
            "verbosity": 3,
        },
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

    # Advance "time" so Member coordinator will update w/ the older data.
    now = dt_now_real() + UPDATE_INTERVAL
    dt_now_mock.return_value = now
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)

    mid = MemberID(cast(str, mem["id"]))

    # The update should be ignored, and reported, exactly once, no matter how many
    # entities the Member has.
    any_pat = re.compile(
        r".*: Ignoring location update because last_seen .* < previous last_seen .*"
    )
    dt_pat = re.compile(
        r"Life360 First1 Last1 \(device_tracker\.life360_first1_last1\): "
        r"Ignoring location update because last_seen .* < previous last_seen .*"
    )
    assert_log_messages(caplog, ((1, "WARNING", any_pat), (1, "WARNING", dt_pat)))

    # Location details should still be from the previous, newer update, ...
    unchanged = {
        "address": "1 Main St",
        "last_seen": dt_util.utc_from_timestamp(LAST_SEEN).isoformat(),
    }
    for key, value in unchanged.items():
        state = hass.states.get(_entity_id(entity_registry, S_DOMAIN, mid, key))
        assert state
        assert state.state == value

    # ... but data that isn't part of the location details is still updated.
    state = hass.states.get(_entity_id(entity_registry, S_DOMAIN, mid, "battery_level"))
    assert state
    assert state.state == "50"

    state = hass.states.get(
        _entity_id(entity_registry, S_DOMAIN, mid, "ignored_update_reasons")
    )
    assert state
    assert state.state == "last_seen"

    # The device_tracker's attributes should agree with the sensors.
    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.attributes["address"] == "1 Main St"
    assert state.attributes["battery_level"] == 50
    assert state.attributes["ignored_update_reasons"] == ["last_seen"]
