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
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
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

MEMBER_NAME = "Life360 First1 Last1"

SENSOR_KEYS = (
    "address",
    "at_loc_since",
    "battery_level",
    "gps_accuracy",
    "ignored_update_reasons",
    "last_seen",
    "latitude",
    "longitude",
    "place",
    "reason",
    "speed",
)
BINARY_SENSOR_KEYS = ("battery_charging", "driving", "wifi_on")
# Entity ID suffixes come from the entity names, which do not always match the
# attribute the entity reports.
ENTITY_ID_SUFFIXES = {"at_loc_since": "at_location_since"}

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

# Member data w/ a newer last_seen & a different address.
moved_mem = deepcopy(mem)
cast(dict[str, Any], moved_mem["location"]).update(
    {"address1": "2 Other St", "timestamp": LAST_SEEN + 60}
)

# Member data for when the Member has stopped sharing their location.
not_sharing_mem = deepcopy(mem)
not_sharing_mem["features"] = {"shareLocation": 0}
not_sharing_mem["location"] = None

renamed_mem = deepcopy(mem)
renamed_mem["firstName"] = "New1"


def _entity_id(
    entity_registry: er.EntityRegistry, domain: str, mid: MemberID, key: str
) -> str:
    """Return entity ID of Member's entity for given attribute."""
    entity_id = entity_registry.async_get_entity_id(domain, DOMAIN, f"{mid}_{key}")
    assert entity_id, f"No {domain} entity for {key}"
    return entity_id


def _state(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, mid: MemberID, key: str
) -> str:
    """Return state of Member's sensor for given attribute."""
    state = hass.states.get(_entity_id(entity_registry, S_DOMAIN, mid, key))
    assert state, f"No state for {key} sensor"
    return state.state


async def _setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration w/ a single account."""
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
            # Use higher verbosity so that API name is AccountID.
            "verbosity": 3,
        },
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

    return entry


async def _next_update(hass: HomeAssistant, dt_now: DtNowMock) -> None:
    """Advance "time" so Member coordinator will update."""
    dt_now_real, dt_now_mock = dt_now
    now = dt_now_real() + UPDATE_INTERVAL
    dt_now_mock.return_value = now
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)


def _api_data(get_circle_member: Any) -> dict[str, Any]:
    """Return mocked API data for one account w/ one Circle & one Member."""
    return {
        "aid1": {
            "get_circles": repeat([cir]),
            "get_circle_members": repeat([mem]),
            "get_circle_member": get_circle_member,
        },
    }


@pytest.mark.parametrize(
    "MockLife360", [_api_data(repeat(mem))], indirect=["MockLife360"]
)
async def test_member_sensors(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test a sensor is created for each of the Member's attributes."""
    await _setup_integration(hass)

    mid = MemberID(cast(str, mem["id"]))
    object_id = MEMBER_NAME.lower().replace(" ", "_")

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
    assert set(expected) | {"speed"} == set(SENSOR_KEYS)
    for key, value in expected.items():
        entity_id = _entity_id(entity_registry, S_DOMAIN, mid, key)
        assert entity_id == f"{S_DOMAIN}.{object_id}_{ENTITY_ID_SUFFIXES.get(key, key)}"
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
    assert set(expected_bs) == set(BINARY_SENSOR_KEYS)
    for key, value in expected_bs.items():
        entity_id = _entity_id(entity_registry, BS_DOMAIN, mid, key)
        state = hass.states.get(entity_id)
        assert state, f"No state for {entity_id}"
        assert state.state == value, f"Unexpected state for {entity_id}"


@pytest.mark.parametrize(
    "MockLife360",
    [_api_data(chain([mem], repeat(old_mem)))],
    indirect=["MockLife360"],
)
async def test_ignored_update(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    dt_now: DtNowMock,
) -> None:
    """Test Member's entities agree when a location update is ignored."""
    await _setup_integration(hass)
    await _next_update(hass, dt_now)

    mid = MemberID(cast(str, mem["id"]))

    # The update should be ignored, and reported, exactly once, no matter how many
    # entities the Member has.
    any_pat = re.compile(
        r".*: Ignoring location update because last_seen .* < previous last_seen .*"
    )
    dt_pat = re.compile(
        rf"{MEMBER_NAME} \(device_tracker\.life360_first1_last1\): "
        r"Ignoring location update because last_seen .* < previous last_seen .*"
    )
    assert_log_messages(caplog, ((1, "WARNING", any_pat), (1, "WARNING", dt_pat)))

    # Location details should still be from the previous, newer update, ...
    assert _state(hass, entity_registry, mid, "address") == "1 Main St"
    assert (
        _state(hass, entity_registry, mid, "last_seen")
        == dt_util.utc_from_timestamp(LAST_SEEN).isoformat()
    )

    # ... but data that isn't part of the location details is still updated.
    assert _state(hass, entity_registry, mid, "battery_level") == "50"
    assert _state(hass, entity_registry, mid, "ignored_update_reasons") == "last_seen"

    # The device_tracker's attributes should agree with the sensors.
    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.attributes["address"] == "1 Main St"
    assert state.attributes["battery_level"] == 50
    assert state.attributes["ignored_update_reasons"] == ["last_seen"]


@pytest.mark.parametrize(
    "MockLife360",
    [_api_data(chain([mem, not_sharing_mem], repeat(moved_mem)))],
    indirect=["MockLife360"],
)
async def test_location_lost_and_regained(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    dt_now: DtNowMock,
) -> None:
    """Test address does not linger when the Member's location goes away."""
    await _setup_integration(hass)

    mid = MemberID(cast(str, mem["id"]))
    assert _state(hass, entity_registry, mid, "address") == "1 Main St"

    # Member stops sharing their location, so there is no address any more.
    await _next_update(hass, dt_now)
    assert _state(hass, entity_registry, mid, "address") == STATE_UNKNOWN
    assert _state(hass, entity_registry, mid, "place") == STATE_UNKNOWN
    assert (
        _state(hass, entity_registry, mid, "reason") == "Member is not sharing location"
    )

    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id
    state = hass.states.get(dt_entity_id)
    assert state
    assert "address" not in state.attributes

    # Member shares their location again, from somewhere else.
    await _next_update(hass, dt_now)
    assert _state(hass, entity_registry, mid, "address") == "2 Other St"
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.attributes["address"] == "2 Other St"


@pytest.mark.parametrize(
    "MockLife360", [_api_data(repeat(mem))], indirect=["MockLife360"]
)
async def test_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test each Member, and the Life360 service, has its own device."""
    entry = await _setup_integration(hass)

    mid = MemberID(cast(str, mem["id"]))

    # The Member's device holds the Member's device tracker & all of their sensors.
    device = device_registry.async_get_device(identifiers={(DOMAIN, mid)})
    assert device
    assert device.name == MEMBER_NAME
    assert device.manufacturer == "Life360"
    entity_ids = {
        entity.entity_id
        for entity in er.async_entries_for_device(entity_registry, device.id)
    }
    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id
    assert entity_ids == {dt_entity_id} | {
        _entity_id(entity_registry, domain, mid, key)
        for domain, keys in ((S_DOMAIN, SENSOR_KEYS), (BS_DOMAIN, BINARY_SENSOR_KEYS))
        for key in keys
    }

    # Entity IDs & friendly names should not have changed by moving to devices.
    assert dt_entity_id == f"{DT_DOMAIN}.life360_first1_last1"
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.attributes[ATTR_FRIENDLY_NAME] == MEMBER_NAME

    state = hass.states.get(_entity_id(entity_registry, S_DOMAIN, mid, "battery_level"))
    assert state
    assert state.attributes[ATTR_FRIENDLY_NAME] == f"{MEMBER_NAME} Battery level"

    # The Life360 service device holds the account online binary sensors.
    device = device_registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device
    assert device.name == "Life360"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    bs_entity_id = entity_registry.async_get_entity_id(BS_DOMAIN, DOMAIN, "aid1")
    assert bs_entity_id == f"{BS_DOMAIN}.life360_online_aid1"
    assert [
        entity.entity_id
        for entity in er.async_entries_for_device(entity_registry, device.id)
    ] == [bs_entity_id]
    state = hass.states.get(bs_entity_id)
    assert state
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Life360 Online (aid1)"


@pytest.mark.parametrize(
    "MockLife360",
    [_api_data(chain([mem], repeat(renamed_mem)))],
    indirect=["MockLife360"],
)
async def test_member_renamed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    dt_now: DtNowMock,
) -> None:
    """Test Member's device is renamed when the Member is."""
    await _setup_integration(hass)

    mid = MemberID(cast(str, mem["id"]))
    device = device_registry.async_get_device(identifiers={(DOMAIN, mid)})
    assert device
    assert device.name == MEMBER_NAME

    await _next_update(hass, dt_now)

    device = device_registry.async_get_device(identifiers={(DOMAIN, mid)})
    assert device
    assert device.name == "Life360 New1 Last1"

    # The entity IDs stay the same, but the friendly names follow the device.
    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id == f"{DT_DOMAIN}.life360_first1_last1"
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Life360 New1 Last1"
