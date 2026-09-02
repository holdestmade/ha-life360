"""Support for Life360 sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .entity import Life360MemberEntity, async_setup_member_entities
from .helpers import MemberID


def _place(entity: Life360Sensor) -> str | None:
    """Return the Place, if any, the Member is in."""
    if not entity.loc:
        return None
    if isinstance(place := entity.loc.details.place, list):
        return ", ".join(place)
    return place


@dataclass(frozen=True, kw_only=True)
class Life360SensorEntityDescription(SensorEntityDescription):
    """Describes a Life360 sensor entity."""

    value_fn: Callable[[Life360Sensor], StateType | datetime]


# One sensor per attribute of the Member's device_tracker entity. The keys match the
# corresponding device_tracker attribute names, as do the entity ID suffixes, which come
# from the names below.
SENSOR_DESCRIPTIONS: tuple[Life360SensorEntityDescription, ...] = (
    Life360SensorEntityDescription(
        key="address",
        name="Address",
        value_fn=lambda entity: entity.address,
    ),
    Life360SensorEntityDescription(
        key="at_loc_since",
        name="At location since",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda entity: entity.loc.details.at_loc_since if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="battery_level",
        name="Battery level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda entity: entity.loc.battery_level if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="gps_accuracy",
        name="GPS accuracy",
        device_class=SensorDeviceClass.DISTANCE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda entity: entity.gps_accuracy if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="ignored_update_reasons",
        name="Ignored update reasons",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda entity: ", ".join(entity.ignored_update_reasons) or None,
    ),
    Life360SensorEntityDescription(
        key="last_seen",
        name="Last seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda entity: entity.loc.details.last_seen if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="latitude",
        name="Latitude",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=5,
        value_fn=lambda entity: entity.loc.details.latitude if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="longitude",
        name="Longitude",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=5,
        value_fn=lambda entity: entity.loc.details.longitude if entity.loc else None,
    ),
    Life360SensorEntityDescription(
        key="place",
        name="Place",
        value_fn=_place,
    ),
    Life360SensorEntityDescription(
        key="reason",
        name="Reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda entity: entity.reason,
    ),
    Life360SensorEntityDescription(
        key="speed",
        name="Speed",
        device_class=SensorDeviceClass.SPEED,
        # Life360 reports speed in MPH; HA converts it per the unit system.
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda entity: entity.loc.details.speed if entity.loc else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    async_setup_member_entities(
        hass,
        entry,
        async_add_entities,
        lambda coordinator, mid: [
            Life360Sensor(coordinator, mid, description)
            for description in SENSOR_DESCRIPTIONS
        ],
    )


class Life360Sensor(Life360MemberEntity, SensorEntity):
    """Life360 Member sensor."""

    entity_description: Life360SensorEntityDescription

    def __init__(
        self,
        coordinator: MemberDataUpdateCoordinator,
        mid: MemberID,
        description: Life360SensorEntityDescription,
    ) -> None:
        """Initialize Life360 sensor."""
        self.entity_description = description
        super().__init__(coordinator, mid)
        self._attr_unique_id = f"{mid}_{description.key}"

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value reported by the sensor."""
        return self.entity_description.value_fn(self)
