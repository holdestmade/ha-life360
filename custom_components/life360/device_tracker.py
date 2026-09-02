"""Support for Life360 device tracking."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ENTITY_MATCH_ALL,
    STATE_NOT_HOME,
    STATE_UNKNOWN,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter
from homeassistant.util.unit_system import METRIC_SYSTEM

from .const import (
    ATTR_ADDRESS,
    ATTR_AT_LOC_SINCE,
    ATTR_DRIVING,
    ATTR_IGNORED_UPDATE_REASONS,
    ATTR_LAST_SEEN,
    ATTR_PLACE,
    ATTR_REASON,
    ATTR_SPEED,
    ATTR_WIFI_ON,
    SIGNAL_UPDATE_LOCATION,
    STATE_DRIVING,
)
from .coordinator import L360ConfigEntry
from .entity import Life360MemberEntity, async_setup_member_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform."""
    entities = async_setup_member_entities(
        hass,
        entry,
        async_add_entities,
        lambda coordinator, mid: [Life360DeviceTracker(coordinator, mid)],
    )

    async def update_location(entity_id: str | list[str]) -> None:
        """Request Member location update."""
        await asyncio.gather(
            *(
                entity.update_location()
                for mem_entities in entities.values()
                for entity in mem_entities
                if entity_id == ENTITY_MATCH_ALL or entity.entity_id in entity_id
            )
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_UPDATE_LOCATION, update_location)
    )


class Life360DeviceTracker(Life360MemberEntity, TrackerEntity):
    """Life360 Device Tracker."""

    _attr_name = None
    _attr_translation_key = "tracker"
    _log_ignored_updates = True
    _warned_loc_unknown = False

    _unrecorded_attributes = frozenset(
        {
            ATTR_ADDRESS,
            ATTR_PLACE,
        }
    )

    @property
    def _metric(self) -> bool:
        """Return if system is configured for Metric."""
        return self.hass.config.units is METRIC_SYSTEM

    @property
    def force_update(self) -> bool:
        """Return True if state updates should be forced.

        Overridden because CoordinatorEntity sets `should_poll` to False,
        which causes TrackerEntity to set `force_update` to True.
        """
        return False

    @property
    def location_accuracy(self) -> float:
        """Return the location accuracy of the device.

        Value in meters.
        """
        return self.gps_accuracy

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        if not self._data.loc:
            return None
        return self._data.loc.details.latitude

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        if not self._data.loc:
            return None
        return self._data.loc.details.longitude

    @property
    def state(self) -> str | None:
        """Return the state of the device."""
        # If location details are missing, set state to "unknown"; "reason" attribute
        # will indicate why (e.g., Member is not sharing location details, etc.)
        if not self._data.loc:
            return STATE_UNKNOWN

        state = super().state
        if state == STATE_NOT_HOME and self._options.driving and self.driving:
            return STATE_DRIVING
        return state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
        if self._data.loc:
            self._warned_loc_unknown = False

            # Speed is returned in MPH. Convert to KPH if system configured for Metric.
            speed = self._data.loc.details.speed
            if self._metric:
                speed = SpeedConverter.convert(
                    speed,
                    UnitOfSpeed.MILES_PER_HOUR,
                    UnitOfSpeed.KILOMETERS_PER_HOUR,
                )

            attrs: dict[str, Any] = {
                ATTR_ADDRESS: self.address,
                ATTR_AT_LOC_SINCE: dt_util.as_local(
                    self._data.loc.details.at_loc_since
                ),
                ATTR_BATTERY_CHARGING: self._data.loc.battery_charging,
                ATTR_BATTERY_LEVEL: self._data.loc.battery_level,
                ATTR_DRIVING: self.driving,
                ATTR_LAST_SEEN: dt_util.as_local(self._data.loc.details.last_seen),
                ATTR_PLACE: self._data.loc.details.place,
                ATTR_SPEED: speed,
                ATTR_WIFI_ON: self._data.loc.wifi_on,
            }
            if self._ignored_update_reasons:
                attrs[ATTR_IGNORED_UPDATE_REASONS] = self._ignored_update_reasons
            return attrs

        reason = self.reason

        if not self._warned_loc_unknown:
            self._warned_loc_unknown = True
            _LOGGER.warning("Location data for %s is missing: %s", self, reason)

        return {ATTR_REASON: reason}

    async def update_location(self) -> None:
        """Request Member location update.

        Typically causes the Member to update every 5 seconds for one minute.
        """
        # Ignore if the entity is disabled
        if not self.enabled:
            return
        await self.coordinator.update_location()

    def _update_basic_attrs(self) -> None:
        """Update basic attributes."""
        super()._update_basic_attrs()
        self._attr_entity_picture = self._data.details.entity_picture
