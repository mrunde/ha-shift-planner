from datetime import date

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import ShiftCoordinator


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def handle_shift(call: ServiceCall) -> None:
        coordinator: ShiftCoordinator | None = hass.data[DOMAIN].get(
            call.data["config_entry_id"]
        )

        if coordinator is None:
            raise ServiceValidationError(
                "This Shift Planner entry is not loaded"
            )

        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.permissions.check_entity(
                coordinator.calendar,
                POLICY_CONTROL,
            ):
                raise Unauthorized(entity_id=coordinator.calendar)

        try:
            day = date.fromisoformat(call.data["date"])
        except ValueError as err:
            raise ServiceValidationError(
                "Use a date in YYYY-MM-DD format"
            ) from err

        if call.service == "delete_events":
            await coordinator.async_delete_events(day, call.context)
        else:
            action = (
                coordinator.async_update_shift
                if call.service == "update_shift"
                else coordinator.async_create_shift
            )
            await action(day, call.data["shift"], call.context)

    day_fields = {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("date"): vol.All(
            cv.string,
            vol.Match(r"^\d{4}-\d{2}-\d{2}$"),
        ),
    }
    schema = vol.Schema({**day_fields, vol.Required("shift"): cv.string})
    for service in ("create_shift", "update_shift"):
        hass.services.async_register(
            DOMAIN, service, handle_shift, schema=schema
        )
    hass.services.async_register(
        DOMAIN, "delete_events", handle_shift, schema=vol.Schema(day_fields)
    )