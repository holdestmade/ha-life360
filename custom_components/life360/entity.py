"""Base entity for Life360 Members."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import suppress
from copy import deepcopy
import logging
from typing import TypeVar, cast

from homeassistant.const import ATTR_GPS_ACCURACY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_LAST_SEEN,
    ATTRIBUTION,
    DOMAIN,
    MANUFACTURER,
    SIGNAL_MEMBERS_CHANGED,
)
from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .helpers import ConfigOptions, LocationData, MemberData, MemberID, NoLocReason

_LOGGER = logging.getLogger(__name__)


class Life360MemberEntity(
    CoordinatorEntity[MemberDataUpdateCoordinator], RestoreEntity
):
    """Base class for entities that represent a Life360 Member.

    Each of a Member's entities keeps, and processes, its own copy of the Member's
    data. Since processing only depends on the config options and the previously
    accepted data, all of a Member's entities always show the same view of the
    Member's data.
    """

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    # All of a Member's entities belong to the Member's device. Note that
    # BaseTrackerEntity types _attr_device_info as None, but device tracker entities
    # can, and do, belong to a device (see, e.g., bmw_connected_drive.)
    _attr_device_info: DeviceInfo  # type: ignore[assignment]
    # Only one of a Member's entities should log ignored location updates.
    _log_ignored_updates = False

    def __init__(self, coordinator: MemberDataUpdateCoordinator, mid: MemberID) -> None:
        """Initialize Life360 Member entity."""
        super().__init__(coordinator)
        self._mid = mid
        self._attr_unique_id = mid
        self._options = ConfigOptions.from_dict(coordinator.config_entry.options)
        self._prev_data = self._data = deepcopy(coordinator.data)
        self._ignored_update_reasons: list[str] = []
        self._addresses: list[str | None] = []
        self._reset_addresses()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mid)},
            manufacturer=MANUFACTURER,
            name=self._device_name,
        )
        self._update_basic_attrs()

        self.async_on_remove(
            coordinator.config_entry.add_update_listener(
                self._async_config_entry_updated
            )
        )

    def __repr__(self) -> str:
        """Return identification string."""
        name = (
            (self.registry_entry and self.registry_entry.name)
            or (
                self.device_entry
                and (self.device_entry.name_by_user or self.device_entry.name)
            )
            or self._device_name
        )
        # Entities other than the device tracker have a name of their own. Get it
        # from the entity description; reading self.name would cache it, possibly
        # before the entity has been added to a platform.
        description = getattr(self, "entity_description", None)
        if description is not None and isinstance(description.name, str):
            name = f"{name} {description.name}"
        return f"{name} ({self.entity_id})"

    @property
    def loc(self) -> LocationData | None:
        """Return the Member's location data, if available."""
        return self._data.loc

    @property
    def address(self) -> str | None:
        """Return where the Member is located."""
        address1: str | None = None
        address2: str | None = None
        with suppress(IndexError):
            address1 = self._addresses[0]
            address2 = self._addresses[1]
        if address1 and address2:
            return " / ".join(sorted([address1, address2]))
        return address1 or address2

    @property
    def driving(self) -> bool:
        """Return if driving."""
        if not self._data.loc:
            return False
        if (driving_speed := self._options.driving_speed) is not None:
            if self._data.loc.details.speed >= driving_speed:
                return True
        return self._data.loc.details.driving

    @property
    def gps_accuracy(self) -> float:
        """Return the accuracy of the Member's location.

        Value in meters.
        """
        if not self._data.loc:
            return 0
        return self._data.loc.details.gps_accuracy

    @property
    def ignored_update_reasons(self) -> list[str]:
        """Return why the last location update, if any, was ignored."""
        return self._ignored_update_reasons

    @property
    def reason(self) -> str | None:
        """Return why the Member's location is unknown, or None if it is known."""
        if self._data.loc:
            return None
        return {
            NoLocReason.NOT_FOUND: "Member no longer in any known Circle",
            NoLocReason.NOT_SET: "Member data could not be retrieved",
            NoLocReason.NOT_SHARING: "Member is not sharing location",
        }.get(self._data.loc_missing, cast(str, self._data.err_msg))

    @property
    def extra_restore_state_data(self) -> MemberData:
        """Return Life360 specific state data to be restored."""
        return self._data

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Restore state if possible.
        if not (last_extra_data := await self.async_get_last_extra_data()):
            return

        last_md = MemberData.from_dict(last_extra_data.as_dict())
        # Address data can be very old. Throw it away so it's not combined with
        # current address data.
        if last_md.loc:
            last_md.loc.details.address = None
        # If no data was actually available for Member (and MemberData was created just
        # based on MemberDetails, either from .storage/life360, or from initial query of
        # Circle Members), then replace current data with restored data.
        if not self._data.loc and self._data.loc_missing is NoLocReason.NOT_SET:
            self._prev_data = self._data = last_md
            return
        self._prev_data = last_md
        self._process_update()

    @callback
    def _handle_coordinator_update(self, config_changed: bool = False) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data == self._data and not config_changed:
            return

        # Since _process_update might overwrite parts of the Member data (e.g., if
        # gps_accuracy is bad), and since the original data needs to be re-processed
        # when a config option changes (e.g., GPS accuracy limit), make a copy of
        # the data before processing it.
        self._data = deepcopy(self.coordinator.data)
        self._update_basic_attrs()
        self._process_update()

        super()._handle_coordinator_update()

    @property
    def _device_name(self) -> str:
        """Return the name of the Member's device."""
        return f"Life360 {self._data.details.name}"

    def _update_basic_attrs(self) -> None:
        """Update attributes that follow the Member's details."""
        # The device exists once the entity has been added, and needs to be renamed if
        # the Member has been renamed since then.
        name = self._device_name
        if (device := self.device_entry) and device.name != name:
            dr.async_get(self.hass).async_update_device(device.id, name=name)

    def _reset_addresses(self) -> None:
        """Reset where the Member is located per the current data."""
        if not self._data.loc:
            self._addresses = []
            return
        address = self._data.loc.details.address
        if address == self._data.loc.details.place:
            address = None
        self._addresses = [address]

    def _process_update(self) -> None:
        """Process new Member data."""
        if not self._data.loc or not self._prev_data.loc:
            # There is nothing to combine the new address, if any, with.
            self._reset_addresses()
            self._prev_data = self._data
            return

        # Check if we should effectively throw out new location data.
        last_seen = self._data.loc.details.last_seen
        prev_seen = self._prev_data.loc.details.last_seen
        max_gps_acc = self._options.max_gps_accuracy
        bad_last_seen = last_seen < prev_seen
        bad_accuracy = max_gps_acc is not None and self.gps_accuracy > max_gps_acc

        if bad_last_seen or bad_accuracy:
            if bad_last_seen and ATTR_LAST_SEEN not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_LAST_SEEN)
                if self._log_ignored_updates:
                    _LOGGER.warning(
                        "%s: Ignoring location update because "
                        "last_seen (%s) < previous last_seen (%s)",
                        self,
                        dt_util.as_local(last_seen),
                        dt_util.as_local(prev_seen),
                    )
            if bad_accuracy and ATTR_GPS_ACCURACY not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_GPS_ACCURACY)
                if self._log_ignored_updates:
                    _LOGGER.warning(
                        "%s: Ignoring location update because "
                        "expected GPS accuracy (%.1f) is not met: %.1f",
                        self,
                        max_gps_acc,
                        self.gps_accuracy,
                    )
            # Overwrite new location details with previous values.
            self._data.loc.details = self._prev_data.loc.details

        else:
            self._ignored_update_reasons.clear()

            if (
                address := self._data.loc.details.address
            ) == self._data.loc.details.place:
                address = None
            if last_seen != prev_seen:
                if address not in self._addresses:
                    self._addresses = [address]
            elif self._data.loc.details.address != self._prev_data.loc.details.address:
                if address not in self._addresses:
                    if len(self._addresses) < 2:
                        self._addresses.append(address)
                    else:
                        self._addresses = [address]

        self._prev_data = self._data

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: L360ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        if self._options == (new_options := ConfigOptions.from_dict(entry.options)):
            return

        old_options = self._options
        self._options = new_options

        need_to_reprocess = any(
            getattr(old_options, attr) != getattr(new_options, attr)
            for attr in ("driving", "driving_speed", "max_gps_accuracy")
        )
        if not need_to_reprocess:
            return

        # Re-process current data.
        self._handle_coordinator_update(config_changed=True)


_MemberEntityT = TypeVar("_MemberEntityT", bound=Life360MemberEntity)


@callback
def async_setup_member_entities(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    create_entities: Callable[
        [MemberDataUpdateCoordinator, MemberID], list[_MemberEntityT]
    ],
) -> dict[MemberID, list[_MemberEntityT]]:
    """Create & delete a platform's entities as Members come & go.

    Returns the platform's entities, keyed by Member ID, which is kept up to date.
    """
    coordinator = entry.runtime_data.coordinator
    mem_coordinator = entry.runtime_data.mem_coordinator
    entities: dict[MemberID, list[_MemberEntityT]] = {}

    def names(entities: Iterable[_MemberEntityT]) -> str:
        """Return the names of the entities, for logging."""
        return ", ".join(str(entity) for entity in entities)

    async def async_process_data() -> None:
        """Process Members."""
        mids = set(coordinator.data.mem_details)
        del_mids = set(entities) - mids
        add_mids = mids - set(entities)

        if del_mids:
            old_entities = [entity for mid in del_mids for entity in entities.pop(mid)]
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Deleting entities: %s", names(old_entities))
            await asyncio.gather(
                *(entity.async_remove() for entity in old_entities if entity.enabled)
            )

        if add_mids:
            new_entities: list[_MemberEntityT] = []
            for mid in add_mids:
                entities[mid] = create_entities(mem_coordinator[mid], mid)
                new_entities.extend(entities[mid])
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Adding entities: %s", names(new_entities))
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    )
    return entities
