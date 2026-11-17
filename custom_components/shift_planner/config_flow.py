import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.helpers import entity_registry as er, selector

from .const import CONF_CALENDAR, DOMAIN


class ShiftPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            entity_id = user_input[CONF_CALENDAR]
            state = self.hass.states.get(entity_id)
            registered = er.async_get(self.hass).async_get(entity_id)

            if not registered or registered.platform != "local_calendar":
                errors["base"] = "not_local_calendar"
            elif state is None or state.state in ("unknown", "unavailable"):
                errors["base"] = "unavailable"
            elif not (
                state.attributes.get("supported_features", 0)
                & CalendarEntityFeature.CREATE_EVENT
            ):
                errors["base"] = "not_writable"
            else:
                await self.async_set_unique_id(registered.id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Shift Planner: {state.name}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_CALENDAR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="calendar",
                        integration="local_calendar",
                    )
                ),
            }),
            errors=errors,
        )