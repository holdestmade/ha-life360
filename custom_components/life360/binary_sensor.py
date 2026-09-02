"""Life360 Binary Sensor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
import logging
from typing import cast

from propcache.api import cached_property

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTRIBUTION,
    DOMAIN,
    MANUFACTURER,
    SIGNAL_ACCT_STATUS,
    SIGNAL_MEMBERS_CHANGED,
)
from .coordinator import (
    CirclesMembersDataUpdateCoordinator,
    L360ConfigEntry,
    MemberDataUpdateCoordinator,
)
from .entity import Life360MemberEntity
from .helpers import AccountID, ConfigOptions, MemberID

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class Life360BinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Life360 Member binary sensor entity."""

    value_fn: Callable[[Life360MemberBinarySensor], bool | None]


# One binary sensor per boolean attribute of the Member's device_tracker entity. The
# keys, and hence the entity ID suffixes, match the corresponding device_tracker
# attribute names.
MEMBER_SENSOR_DESCRIPTIONS: tuple[Life360BinarySensorEntityDescription, ...] = (
    Life360BinarySensorEntityDescription(
        key="battery_charging",
        name="Battery charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda entity: entity.loc.battery_charging if entity.loc else None,
    ),
    Life360BinarySensorEntityDescription(
        key="driving",
        name="Driving",
        device_class=BinarySensorDeviceClass.MOVING,
        value_fn=lambda entity: entity.driving if entity.loc else None,
    ),
    Life360BinarySensorEntityDescription(
        key="wifi_on",
        name="WiFi on",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda entity: entity.loc.wifi_on if entity.loc else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensory platform."""
    coordinator = entry.runtime_data.coordinator
    mem_coordinator = entry.runtime_data.mem_coordinator
    entities: dict[AccountID, Life360BinarySensor] = {}
    mem_entities: dict[MemberID, list[Life360MemberBinarySensor]] = {}

    async def process_config(hass: HomeAssistant, entry: L360ConfigEntry) -> None:
        """Add and/or remove binary online sensors."""
        options = ConfigOptions.from_dict(entry.options)
        aids = set(options.accounts)
        cur_aids = set(entities)
        del_aids = cur_aids - aids
        add_aids = aids - cur_aids

        if del_aids:
            old_entities = [entities.pop(aid) for aid in del_aids]
            _LOGGER.debug("Deleting binary online sensors for: %s", ", ".join(del_aids))
            await asyncio.gather(*(entity.async_remove() for entity in old_entities))

        if add_aids:
            new_entities = {
                aid: Life360BinarySensor(coordinator, aid) for aid in add_aids
            }
            entities.update(new_entities)
            _LOGGER.debug("Adding binary online sensors for: %s", ", ".join(add_aids))
            async_add_entities(new_entities.values())

    async def async_process_data() -> None:
        """Add and/or remove Member binary sensors."""
        mids = set(coordinator.data.mem_details)
        cur_mids = set(mem_entities)
        del_mids = cur_mids - mids
        add_mids = mids - cur_mids

        if del_mids:
            old_entities: list[Life360MemberBinarySensor] = []
            for mid in del_mids:
                old_entities.extend(mem_entities.pop(mid))
            _LOGGER.debug(
                "Deleting binary sensors: %s",
                ", ".join(str(entity) for entity in old_entities),
            )
            await asyncio.gather(
                *(entity.async_remove() for entity in old_entities if entity.enabled)
            )

        if add_mids:
            new_entities: list[Life360MemberBinarySensor] = []
            for mid in add_mids:
                entities_for_mid = [
                    Life360MemberBinarySensor(mem_coordinator[mid], mid, description)
                    for description in MEMBER_SENSOR_DESCRIPTIONS
                ]
                mem_entities[mid] = entities_for_mid
                new_entities.extend(entities_for_mid)
            _LOGGER.debug(
                "Adding binary sensors: %s",
                ", ".join(str(entity) for entity in new_entities),
            )
            async_add_entities(new_entities)

    await process_config(hass, entry)
    entry.async_on_unload(entry.add_update_listener(process_config))
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    )


class Life360BinarySensor(BinarySensorEntity):
    """Life360 Binary Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "online"

    def __init__(
        self, coordinator: CirclesMembersDataUpdateCoordinator, aid: AccountID
    ) -> None:
        """Initialize binary sensor."""
        self._attr_translation_placeholders = {"acct_id": aid}
        self._attr_unique_id = aid
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            manufacturer=MANUFACTURER,
            name=MANUFACTURER,
        )
        self._enabled = (
            ConfigOptions.from_dict(coordinator.config_entry.options)
            .accounts[aid]
            .enabled
        )
        self._online = partial(coordinator.acct_online, aid)

        self.async_on_remove(
            coordinator.config_entry.add_update_listener(
                self._async_config_entry_updated
            )
        )

    @cached_property
    def aid(self) -> AccountID:
        """Return account ID."""
        return cast(AccountID, self.unique_id)

    @property
    def is_on(self) -> bool:
        """Return if account is online."""
        if not self._enabled:
            return False
        return self._online()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""

        @callback
        def write_state(aid: AccountID) -> None:
            """Write state if account status was updated."""
            if aid == self.aid:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ACCT_STATUS, write_state)
        )

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: L360ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        # Check to make sure account hasn't just been deleted. If so, don't update state
        # because we're about to be removed.
        if not (acct := ConfigOptions.from_dict(entry.options).accounts.get(self.aid)):
            return
        if acct.enabled == self._enabled:
            return

        self._enabled = not self._enabled
        self.async_write_ha_state()


class Life360MemberBinarySensor(Life360MemberEntity, BinarySensorEntity):
    """Life360 Member binary sensor."""

    entity_description: Life360BinarySensorEntityDescription

    def __init__(
        self,
        coordinator: MemberDataUpdateCoordinator,
        mid: MemberID,
        description: Life360BinarySensorEntityDescription,
    ) -> None:
        """Initialize Life360 Member binary sensor."""
        self.entity_description = description
        super().__init__(coordinator, mid)
        self._attr_unique_id = f"{mid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return if the binary sensor is on."""
        return self.entity_description.value_fn(self)
