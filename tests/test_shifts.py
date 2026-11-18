import asyncio
from datetime import date, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ServiceValidationError

from custom_components.shift_planner.config_flow import ShiftPlannerOptionsFlow
from custom_components.shift_planner.const import DEFAULT_SHIFTS
from custom_components.shift_planner.coordinator import ShiftCoordinator
from custom_components.shift_planner.services import async_setup_services


class TestOptionsFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(options={})

        class Flow(ShiftPlannerOptionsFlow):
            @property
            def config_entry(flow_self):
                return self.entry

        self.flow = Flow()
        self.flow.async_show_menu = lambda **kwargs: kwargs
        self.flow.async_show_form = lambda **kwargs: kwargs
        self.flow.async_create_entry = lambda **kwargs: kwargs

    async def test_add_edit_and_remove(self):
        self.assertEqual(
            (await self.flow.async_step_init())["menu_options"],
            ["add", "edit", "remove"],
        )
        result = await self.flow.async_step_add(
            {"name": " Night ", "start": "22:00:00", "end": "06:00:00"}
        )
        self.entry.options = result["data"]
        self.assertEqual(self.entry.options["shifts"]["Night"]["label"], "Night")
        self.assertEqual(len(DEFAULT_SHIFTS), 4)

        result = await self.flow.async_step_edit({"shift": "Night"})
        self.assertEqual(result["step_id"], "edit_shift")
        result = await self.flow.async_step_edit_shift(
            {"name": "Late", "start": "21:00:00", "end": "05:00:00"}
        )
        self.entry.options = result["data"]
        self.assertNotIn("Night", self.entry.options["shifts"])
        self.assertEqual(self.entry.options["shifts"]["Late"]["start"], "21:00:00")

        result = await self.flow.async_step_remove({"shift": "Late"})
        self.assertNotIn("Late", result["data"]["shifts"])
        self.entry.options = {"shifts": {}}
        self.assertEqual((await self.flow.async_step_init())["menu_options"], ["add"])

    async def test_validation(self):
        cases = [
            ({"name": " ", "start": "07:00", "end": "08:00"}, "name", "empty_name"),
            ({"name": "11:00", "start": "07:00", "end": "08:00"}, "name", "duplicate_name"),
            ({"name": "x", "start": "07:00", "end": "07:00:00"}, "base", "same_time"),
        ]
        for values, field, error in cases:
            with self.subTest(error=error):
                result = await self.flow.async_step_add(values)
                self.assertEqual(result["errors"][field], error)

    async def test_edit_preserves_legacy_label(self):
        await self.flow.async_step_edit({"shift": "Frühschicht"})
        result = await self.flow.async_step_edit_shift(
            {"name": "Frühschicht", "start": "06:00", "end": "14:30"}
        )
        self.assertEqual(result["data"]["shifts"]["Frühschicht"]["label"], "Früh")


class TestShiftCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(
            data={"calendar": "calendar.shifts"}, unique_id=None, options={}
        )
        self.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
        self.coordinator = ShiftCoordinator.__new__(ShiftCoordinator)
        self.coordinator.hass = self.hass
        self.coordinator.entry = self.entry
        self.coordinator.lock = asyncio.Lock()
        self.coordinator._events = AsyncMock(return_value=[])
        self.coordinator.async_request_refresh = AsyncMock()
        self.today = date(2026, 11, 18)
        self.patch_monday = patch(
            "custom_components.shift_planner.coordinator.monday",
            return_value=date(2026, 11, 16),
        )
        self.patch_timezone = patch(
            "custom_components.shift_planner.coordinator.dt_util.DEFAULT_TIME_ZONE",
            timezone.utc,
        )
        self.patch_monday.start()
        self.patch_timezone.start()
        self.addCleanup(self.patch_monday.stop)
        self.addCleanup(self.patch_timezone.stop)

    async def test_default_and_overnight_times(self):
        await self.coordinator.async_create_shift(self.today, "Frühschicht", None)
        data = self.hass.services.async_call.await_args.args[2]
        self.assertEqual(data["start_date_time"], "2026-11-18T07:00:00+00:00")
        self.assertEqual(data["end_date_time"], "2026-11-18T15:30:00+00:00")

        self.entry.options = {
            "shifts": {"Night": {"label": "Night", "start": "22:00", "end": "06:00"}}
        }
        await self.coordinator.async_create_shift(self.today, "Night", None)
        data = self.hass.services.async_call.await_args.args[2]
        self.assertEqual(data["summary"], "Night")
        self.assertEqual(data["end_date_time"], "2026-11-19T06:00:00+00:00")

    async def test_reject_unknown_shift_invalid_date_and_existing_event(self):
        with self.assertRaises(ServiceValidationError):
            await self.coordinator.async_create_shift(self.today, "Missing", None)
        with self.assertRaises(ServiceValidationError):
            await self.coordinator.async_create_shift(date(2026, 12, 31), "11:00", None)
        self.coordinator._events.return_value = [{"summary": "Existing"}]
        with self.assertRaises(ServiceValidationError):
            await self.coordinator.async_create_shift(self.today, "11:00", None)
        self.hass.services.async_call.assert_not_awaited()

    async def test_options_changed_during_event_fetch(self):
        async def fetch(*args):
            self.entry.options = {"shifts": {}}
            return []

        self.coordinator._events = fetch
        await self.coordinator.async_create_shift(self.today, "11:00", None)
        data = self.hass.services.async_call.await_args.args[2]
        self.assertEqual(data["end_date_time"], "2026-11-18T19:30:00+00:00")

    def test_service_schema_accepts_configured_names(self):
        hass = SimpleNamespace(services=MagicMock())
        async_setup_services(hass)
        schema = hass.services.async_register.call_args.kwargs["schema"]
        data = schema({"config_entry_id": "id", "date": "2026-11-18", "shift": "Night"})
        self.assertEqual(data["shift"], "Night")
