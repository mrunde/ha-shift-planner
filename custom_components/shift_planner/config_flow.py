from datetime import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import CONF_CALENDAR, CONF_SHIFTS, DEFAULT_SHIFTS, DOMAIN


class ShiftPlannerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ShiftPlannerOptionsFlow()

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


class ShiftPlannerOptionsFlow(config_entries.OptionsFlow):
    def __init__(self):
        self._selected_shift = None

    @property
    def _shifts(self):
        return self.config_entry.options.get(CONF_SHIFTS, DEFAULT_SHIFTS)

    async def async_step_init(self, user_input=None):
        menu = ["add"]
        if self._shifts:
            menu.extend(["edit", "remove"])
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add(self, user_input=None):
        return self._shift_form("add", user_input)

    async def async_step_edit(self, user_input=None):
        if user_input is not None:
            self._selected_shift = user_input["shift"]
            return await self.async_step_edit_shift()
        return self._select_form("edit")

    async def async_step_edit_shift(self, user_input=None):
        return self._shift_form("edit_shift", user_input, self._selected_shift)

    async def async_step_remove(self, user_input=None):
        if user_input is not None:
            shifts = dict(self._shifts)
            shifts.pop(user_input["shift"])
            return self._save(shifts)
        return self._select_form("remove")

    def _select_form(self, step_id):
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({
                vol.Required("shift"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(self._shifts))
                ),
            }),
        )

    def _shift_form(self, step_id, user_input, original=None):
        errors = {}
        if user_input is not None:
            name = user_input["name"].strip()
            if not name:
                errors["name"] = "empty_name"
            elif name != original and name in self._shifts:
                errors["name"] = "duplicate_name"
            elif time.fromisoformat(user_input["start"]) == time.fromisoformat(
                user_input["end"]
            ):
                errors["base"] = "same_time"
            else:
                shifts = dict(self._shifts)
                previous = shifts.pop(original, {}) if original is not None else {}
                shifts[name] = {
                    "label": previous.get("label", name) if name == original else name,
                    "start": user_input["start"],
                    "end": user_input["end"],
                }
                return self._save(shifts)

        current = self._shifts.get(original, {})
        values = user_input or {
            "name": original or "",
            "start": current.get("start", "07:00:00"),
            "end": current.get("end", "15:30:00"),
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({
                vol.Required("name", default=values["name"]): str,
                vol.Required("start", default=values["start"]): selector.TimeSelector(),
                vol.Required("end", default=values["end"]): selector.TimeSelector(),
            }),
            errors=errors,
        )

    def _save(self, shifts):
        return self.async_create_entry(
            title="",
            data={**self.config_entry.options, CONF_SHIFTS: shifts},
        )