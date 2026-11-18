from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ShiftCoordinator


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        ShiftSensor(hass.data[DOMAIN][entry.entry_id])
    ])


class ShiftSensor(CoordinatorEntity[ShiftCoordinator], SensorEntity):
    _attr_name = "Shift Plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: ShiftCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.entry.entry_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def midnight_refresh(now):
            self.hass.async_create_task(
                self.coordinator.async_request_refresh()
            )

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                midnight_refresh,
                hour=0,
                minute=0,
                second=0,
            )
        )

    @property
    def native_value(self):
        return sum(
            bool(day["summaries"])
            for day in self.coordinator.data["days"]
        )

    @property
    def extra_state_attributes(self):
        return {
            **self.coordinator.data,
            "config_entry_id": self.coordinator.entry.entry_id,
            "calendar": self.coordinator.calendar,
            "shifts": [
                {"id": key, **value}
                for key, value in self.coordinator.shifts.items()
            ],
        }