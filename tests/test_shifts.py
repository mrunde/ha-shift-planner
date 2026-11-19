import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from ical.calendar import Calendar
from ical.calendar_stream import IcsCalendarStream
from ical.event import Event
from ical.types import Recur
import voluptuous as vol

from homeassistant.components.calendar import CalendarEntityFeature, CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.components.local_calendar.calendar import LocalCalendarEntity
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)

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
        menu = await self.flow.async_step_init()
        self.assertEqual(
            menu["menu_options"],
            ["add", "edit", "remove"],
        )
        self.assertEqual(menu["description_placeholders"]["shift_count"], "4")
        result = await self.flow.async_step_add(
            {"name": " Night ", "start": "22:00:00", "end": "06:00:00"}
        )
        self.entry.options = result["data"]
        self.assertEqual(self.entry.options["shifts"]["Night"]["label"], "Night")
        self.assertEqual(len(DEFAULT_SHIFTS), 4)

        result = await self.flow.async_step_edit({"shift": "Night"})
        self.assertEqual(result["step_id"], "edit_shift")
        result = await self.flow.async_step_edit_shift(
            {"name": "Late Night", "start": "21:00:00", "end": "05:00:00"}
        )
        self.entry.options = result["data"]
        self.assertNotIn("Night", self.entry.options["shifts"])
        self.assertEqual(self.entry.options["shifts"]["Late Night"]["start"], "21:00:00")

        result = await self.flow.async_step_remove({"shift": "Late Night"})
        self.assertNotIn("Late Night", result["data"]["shifts"])
        self.entry.options = {"shifts": {}}
        menu = await self.flow.async_step_init()
        self.assertEqual(menu["menu_options"], ["add"])
        self.assertEqual(menu["description_placeholders"]["shift_count"], "0")

    async def test_shift_picker_shows_hours_without_changing_value(self):
        self.entry.options = {
            "shifts": {"Night": {"label": "Night", "start": "22:00:00", "end": "06:00"}}
        }
        for step, last_step in (("edit", False), ("remove", True)):
            with self.subTest(step=step):
                result = await getattr(self.flow, f"async_step_{step}")()
                self.assertEqual(result["last_step"], last_step)
                schema = result["data_schema"]
                shift_selector = next(iter(schema.schema.values()))
                self.assertEqual(
                    shift_selector.config["options"],
                    [{"value": "Night", "label": "Night (22:00 - 06:00)"}],
                )
                self.assertEqual(schema({"shift": "Night"}), {"shift": "Night"})

    async def test_options_translations_cover_menu_and_forms(self):
        translations_dir = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "shift_planner"
            / "translations"
        )
        menu = await self.flow.async_step_init()
        for language in ("en", "de"):
            with self.subTest(language=language):
                translations = json.loads(
                    (translations_dir / f"{language}.json").read_text(encoding="utf-8")
                )
                options = translations["options"]
                steps = options["step"]
                init = steps[menu["step_id"]]
                self.assertTrue(init["title"].strip())
                self.assertIn("{shift_count}", init["description"])
                for action in menu["menu_options"]:
                    self.assertTrue(init["menu_options"][action].strip())
                    self.assertTrue(init["menu_option_descriptions"][action].strip())
                for step in ("add", "edit", "edit_shift", "remove"):
                    self.assertTrue(steps[step]["title"].strip())
                    self.assertTrue(steps[step]["description"].strip())
                for error in ("empty_name", "duplicate_name", "same_time"):
                    self.assertTrue(options["error"][error].strip())

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
        self.entry.options = {
            "shifts": {
                "Frühschicht": {"label": "Früh", "start": "07:00", "end": "15:30"}
            }
        }
        await self.flow.async_step_edit({"shift": "Frühschicht"})
        result = await self.flow.async_step_edit_shift(
            {"name": "Frühschicht", "start": "06:00", "end": "14:30"}
        )
        self.assertEqual(result["data"]["shifts"]["Frühschicht"]["label"], "Früh")


class TestShiftOperations(unittest.IsolatedAsyncioTestCase):
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
        self.event = CalendarEvent(
            start=datetime(2026, 11, 18, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 11, 18, 15, tzinfo=timezone.utc),
            summary="Early",
            uid="existing-event",
            description="Keep these notes",
            location="Office",
        )
        self.calendar = MagicMock(
            supported_features=CalendarEntityFeature.UPDATE_EVENT,
            async_get_events=AsyncMock(return_value=[self.event]),
            async_update_event=AsyncMock(),
            async_delete_event=AsyncMock(),
        )
        self.hass.states = MagicMock()
        self.hass.states.get.return_value.state = "on"
        component = MagicMock()
        component.get_entity.return_value = self.calendar
        self.hass.data = {DATA_COMPONENT: component}

    async def test_default_and_overnight_times(self):
        self.assertEqual(list(self.coordinator.shifts), [
            "Early",
            "11:00",
            "12:00",
            "Late",
        ])

        await self.coordinator.async_create_shift(self.today, "Early", None)
        data = self.hass.services.async_call.await_args.args[2]
        self.assertEqual(data["start_date_time"], "2026-11-18T06:30:00+00:00")
        self.assertEqual(data["end_date_time"], "2026-11-18T15:00:00+00:00")

        self.entry.options = {
            "shifts": {"Night": {"label": "Night", "start": "22:00", "end": "06:00"}}
        }
        await self.coordinator.async_create_shift(self.today, "Night", None)
        data = self.hass.services.async_call.await_args.args[2]
        self.assertEqual(data["summary"], "Night")
        self.assertEqual(data["end_date_time"], "2026-11-19T06:00:00+00:00")

    async def test_calendar_shifts_are_returned_by_start_time(self):
        self.coordinator._events = AsyncMock(return_value=[
            {
                "start": "2026-11-18T14:00:00+00:00",
                "end": "2026-11-18T22:30:00+00:00",
                "summary": "Late",
            },
            {
                "start": "2026-11-18T06:30:00+00:00",
                "end": "2026-11-18T15:00:00+00:00",
                "summary": "Early",
            },
            {
                "start": "2026-11-18T11:00:00+00:00",
                "end": "2026-11-18T19:30:00+00:00",
                "summary": "Mid",
            },
        ])

        data = await self.coordinator._async_update_data()

        self.assertEqual(data["days"][2]["summaries"], ["Early", "Mid", "Late"])

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

    async def test_update_changes_name_and_times_in_place(self):
        context = MagicMock()
        await self.coordinator.async_update_shift(self.today, "Late", context)

        self.calendar.async_update_event.assert_awaited_once_with(
            "existing-event",
            {
                "summary": "Late",
                "dtstart": datetime(2026, 11, 18, 14, tzinfo=timezone.utc),
                "dtend": datetime(2026, 11, 18, 22, 30, tzinfo=timezone.utc),
                "description": "Keep these notes",
                "location": "Office",
            },
            recurrence_id=None,
        )
        self.calendar.async_set_context.assert_called_once_with(context)
        self.hass.services.async_call.assert_not_awaited()
        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_update_to_overnight_shift_and_back(self):
        self.entry.options = {
            "shifts": {
                **DEFAULT_SHIFTS,
                "Night": {"label": "Night", "start": "22:00", "end": "06:00"},
            }
        }
        await self.coordinator.async_update_shift(self.today, "Night", None)
        data = self.calendar.async_update_event.await_args.args[1]
        self.assertEqual(data["dtstart"].isoformat(), "2026-11-18T22:00:00+00:00")
        self.assertEqual(data["dtend"].isoformat(), "2026-11-19T06:00:00+00:00")

        self.event.start = data["dtstart"]
        self.event.end = data["dtend"]
        await self.coordinator.async_update_shift(self.today, "Early", None)
        data = self.calendar.async_update_event.await_args.args[1]
        self.assertEqual(data["dtstart"].isoformat(), "2026-11-18T06:30:00+00:00")
        self.assertEqual(data["dtend"].isoformat(), "2026-11-18T15:00:00+00:00")

    async def test_update_only_selected_recurring_occurrence(self):
        self.event.rrule = "FREQ=DAILY"
        self.event.recurrence_id = "20261118T063000"
        await self.coordinator.async_update_shift(self.today, "Late", None)
        call = self.calendar.async_update_event.await_args
        self.assertEqual(call.kwargs, {"recurrence_id": "20261118T063000"})
        self.assertNotIn("rrule", call.args[1])

    async def test_update_rejects_unknown_shift_and_outside_planning_window(self):
        for day, shift in (
            (self.today, "Missing"),
            (date(2026, 11, 15), "Late"),
            (date(2026, 11, 30), "Late"),
        ):
            with self.subTest(day=day, shift=shift):
                with self.assertRaises(ServiceValidationError):
                    await self.coordinator.async_update_shift(day, shift, None)
        self.calendar.async_get_events.assert_not_awaited()
        self.calendar.async_update_event.assert_not_awaited()

    async def test_update_rejects_missing_or_ambiguous_events(self):
        for events in ([], [self.event, self.event]):
            with self.subTest(count=len(events)):
                self.calendar.async_get_events.return_value = events
                with self.assertRaisesRegex(ServiceValidationError, "exactly one"):
                    await self.coordinator.async_update_shift(self.today, "Late", None)
        self.calendar.async_update_event.assert_not_awaited()
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_update_requires_start_date_and_uid(self):
        self.event.start = datetime(2026, 11, 17, 22, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ServiceValidationError, "event starts"):
            await self.coordinator.async_update_shift(self.today, "Late", None)
        self.event.start = self.today
        self.event.uid = None
        with self.assertRaisesRegex(ServiceValidationError, "no UID"):
            await self.coordinator.async_update_shift(self.today, "Late", None)
        self.calendar.async_update_event.assert_not_awaited()

    async def test_update_all_day_event(self):
        self.event.start = self.today
        self.event.end = date(2026, 11, 19)
        await self.coordinator.async_update_shift(self.today, "Late", None)
        data = self.calendar.async_update_event.await_args.args[1]
        self.assertEqual(data["dtstart"].isoformat(), "2026-11-18T14:00:00+00:00")

    async def test_update_requires_available_loaded_editable_calendar(self):
        self.hass.states.get.return_value.state = "unavailable"
        with self.assertRaisesRegex(HomeAssistantError, "unavailable"):
            await self.coordinator.async_update_shift(self.today, "Late", None)
        self.hass.states.get.return_value.state = "on"
        self.calendar.supported_features = CalendarEntityFeature.CREATE_EVENT
        with self.assertRaisesRegex(ServiceValidationError, "does not support"):
            await self.coordinator.async_update_shift(self.today, "Late", None)
        self.hass.data = {}
        with self.assertRaisesRegex(HomeAssistantError, "not loaded"):
            await self.coordinator.async_update_shift(self.today, "Late", None)
        self.calendar.async_update_event.assert_not_awaited()

    async def test_update_surfaces_calendar_errors_without_refresh(self):
        for error in (HomeAssistantError("Write failed"), ValueError("Invalid event")):
            with self.subTest(error=error):
                self.calendar.async_update_event.side_effect = error
                with self.assertRaises(HomeAssistantError):
                    await self.coordinator.async_update_shift(self.today, "Late", None)
        self.coordinator.async_request_refresh.assert_not_awaited()

    def cleanup_event(self, uid, start, end=None, **kwargs):
        return CalendarEvent(
            uid=uid,
            summary="Calendar event",
            start=start,
            end=end or start + timedelta(days=1),
            **kwargs,
        )

    async def test_cleanup_deletes_all_history_by_start_not_end(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        old_events = [
            self.cleanup_event("ancient", date(1900, 1, 1)),
            self.cleanup_event("last-week", date(2026, 11, 9)),
            self.cleanup_event(
                "overnight",
                datetime(2026, 11, 15, 22, tzinfo=timezone.utc),
                datetime(2026, 11, 16, 6, tzinfo=timezone.utc),
            ),
            self.cleanup_event(
                "long-event", date(2026, 11, 1), date(2026, 12, 1)
            ),
        ]
        kept_events = [
            self.cleanup_event("monday", date(2026, 11, 16)),
            self.cleanup_event(
                "exact-cutoff", datetime(2026, 11, 16, tzinfo=timezone.utc)
            ),
            self.event,
            self.cleanup_event("future", date(2030, 1, 1)),
        ]
        self.calendar.async_get_events.return_value = old_events + kept_events
        context = MagicMock()

        await self.coordinator.async_delete_old_events(context)

        self.calendar.async_get_events.assert_awaited_once_with(
            self.hass,
            datetime(1, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [call.args[0] for call in self.calendar.async_delete_event.await_args_list],
            [event.uid for event in old_events],
        )
        for call in self.calendar.async_delete_event.await_args_list:
            self.assertEqual(call.kwargs, {"recurrence_id": None})
        self.calendar.async_set_context.assert_called_once_with(context)
        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_cleanup_uses_local_week_boundary(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        for zone in ("Europe/Berlin", "America/New_York"):
            with self.subTest(zone=zone), patch(
                "custom_components.shift_planner.coordinator.dt_util.DEFAULT_TIME_ZONE",
                ZoneInfo(zone),
            ):
                local = ZoneInfo(zone)
                cutoff = datetime(2026, 11, 16, tzinfo=local)
                self.calendar.async_delete_event.reset_mock()
                self.calendar.async_get_events.return_value = [
                    self.cleanup_event(
                        "before", (cutoff - timedelta(seconds=1)).astimezone(timezone.utc)
                    ),
                    self.cleanup_event("at", cutoff.astimezone(timezone.utc)),
                    self.cleanup_event("all-day-before", date(2026, 11, 15)),
                    self.cleanup_event("all-day-at", date(2026, 11, 16)),
                ]
                await self.coordinator.async_delete_old_events(None)
                self.assertEqual(
                    [
                        call.args[0]
                        for call in self.calendar.async_delete_event.await_args_list
                    ],
                    ["before", "all-day-before"],
                )
                start = self.calendar.async_get_events.await_args.args[1]
                self.assertEqual(start.astimezone(timezone.utc).year, 1)
                self.assertEqual(start.tzinfo, local)
                self.assertEqual(
                    self.calendar.async_get_events.await_args.args[2], cutoff
                )

    async def test_cleanup_deletes_only_past_recurring_occurrences(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        self.calendar.async_get_events.return_value = [
            self.cleanup_event(
                "series",
                date(2026, 11, day),
                rrule="FREQ=DAILY",
                recurrence_id=f"202611{day}",
            )
            for day in (14, 15, 16, 17)
        ]
        await self.coordinator.async_delete_old_events(None)
        self.assertEqual(self.calendar.async_delete_event.await_count, 2)
        self.assertEqual(
            [
                call.kwargs
                for call in self.calendar.async_delete_event.await_args_list
            ],
            [{"recurrence_id": "20261114"}, {"recurrence_id": "20261115"}],
        )

    async def test_cleanup_prevalidates_events_before_deleting(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        valid = self.cleanup_event("valid", date(2026, 11, 14))
        invalid_events = [
            self.cleanup_event(None, date(2026, 11, 15)),
            self.cleanup_event("series", date(2026, 11, 15), rrule="FREQ=DAILY"),
        ]
        for invalid in invalid_events:
            with self.subTest(invalid=invalid):
                self.calendar.async_get_events.return_value = [valid, invalid]
                with self.assertRaises(ServiceValidationError):
                    await self.coordinator.async_delete_old_events(None)
        self.calendar.async_delete_event.assert_not_awaited()

    async def test_cleanup_empty_or_current_calendar_is_unchanged(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        for events in ([], [self.event]):
            self.calendar.async_get_events.return_value = events
            await self.coordinator.async_delete_old_events(None)
        self.calendar.async_delete_event.assert_not_awaited()

    async def test_cleanup_requires_available_loaded_deletable_calendar(self):
        with self.assertRaisesRegex(ServiceValidationError, "does not support"):
            await self.coordinator.async_delete_old_events(None)
        self.hass.states.get.return_value.state = "unavailable"
        with self.assertRaisesRegex(HomeAssistantError, "unavailable"):
            await self.coordinator.async_delete_old_events(None)
        self.hass.states.get.return_value.state = "on"
        self.hass.data = {}
        with self.assertRaisesRegex(HomeAssistantError, "not loaded"):
            await self.coordinator.async_delete_old_events(None)
        self.calendar.async_delete_event.assert_not_awaited()

    async def test_cleanup_reports_partial_failure_and_refreshes(self):
        self.calendar.supported_features |= CalendarEntityFeature.DELETE_EVENT
        self.calendar.async_get_events.return_value = [
            self.cleanup_event(str(day), date(2026, 11, day))
            for day in (13, 14, 15)
        ]
        self.calendar.async_delete_event.side_effect = [
            None, HomeAssistantError("Write failed")
        ]
        with self.assertRaisesRegex(HomeAssistantError, "after deleting 1 events"):
            await self.coordinator.async_delete_old_events(None)
        self.assertEqual(self.calendar.async_delete_event.await_count, 2)
        self.coordinator.async_request_refresh.assert_awaited_once()
        self.assertFalse(self.coordinator.lock.locked())

    async def test_cleanup_with_real_local_calendar_preserves_future_series(self):
        for zone in ("Europe/Berlin", "America/New_York"):
            with self.subTest(zone=zone), patch(
                "custom_components.shift_planner.coordinator.dt_util.DEFAULT_TIME_ZONE",
                ZoneInfo(zone),
            ):
                calendar = Calendar(events=[
                    Event(
                        uid="daily",
                        summary="Daily shift",
                        start=datetime(2026, 11, 9, 6, 30),
                        end=datetime(2026, 11, 9, 15),
                        rrule=Recur.from_rrule("FREQ=DAILY"),
                    ),
                    Event(
                        uid="all-day-series",
                        summary="All day",
                        start=date(2026, 11, 9),
                        end=date(2026, 11, 10),
                        rrule=Recur.from_rrule("FREQ=DAILY"),
                    ),
                    Event(
                        uid="very-old",
                        summary="Old event",
                        start=date(1900, 1, 1),
                        end=date(1900, 1, 2),
                    ),
                    Event(
                        uid="overnight",
                        summary="Overnight",
                        start=datetime(2026, 11, 15, 22),
                        end=datetime(2026, 11, 16, 6),
                    ),
                    Event(
                        uid="monday",
                        summary="Monday",
                        start=datetime(2026, 11, 16),
                        end=datetime(2026, 11, 16, 1),
                    ),
                    Event(
                        uid="future",
                        summary="Future",
                        start=date(2030, 1, 1),
                        end=date(2030, 1, 2),
                    ),
                ])
                store = SimpleNamespace(async_store=AsyncMock())
                entity = LocalCalendarEntity(store, calendar, "Shifts", "calendar-id")
                entity.hass = self.hass
                entity.async_update_ha_state = AsyncMock()
                self.hass.async_add_executor_job = AsyncMock(
                    side_effect=lambda function, *args: function(*args)
                )
                self.hass.data[DATA_COMPONENT].get_entity.return_value = entity
                local = ZoneInfo(zone)
                cutoff = datetime(2026, 11, 16, tzinfo=local)
                future_end = datetime(2030, 1, 3, tzinfo=local)
                before = await entity.async_get_events(self.hass, cutoff, future_end)
                expected = [
                    event for event in before if event.start_datetime_local >= cutoff
                ]

                await self.coordinator.async_delete_old_events(None)

                after = await entity.async_get_events(self.hass, cutoff, future_end)
                self.assertEqual(after, expected)
                remaining_old = await entity.async_get_events(
                    self.hass, datetime(1899, 1, 1, tzinfo=local), cutoff
                )
                self.assertEqual(remaining_old, [])
                self.assertEqual(store.async_store.await_count, 16)
                persisted = IcsCalendarStream.calendar_from_ics(
                    store.async_store.await_args.args[0]
                )
                reloaded = LocalCalendarEntity(
                    store, persisted, "Shifts", "calendar-id"
                )
                reloaded.hass = self.hass
                self.assertEqual(
                    await reloaded.async_get_events(self.hass, cutoff, future_end),
                    expected,
                )
                await self.coordinator.async_delete_old_events(None)
                self.assertEqual(store.async_store.await_count, 16)

    def test_service_schema_accepts_configured_names(self):
        hass = SimpleNamespace(services=MagicMock())
        async_setup_services(hass)
        registrations = hass.services.async_register.call_args_list
        self.assertEqual(
            [call.args[1] for call in registrations],
            ["create_shift", "update_shift", "delete_old_events"],
        )
        for call in registrations:
            schema = call.kwargs["schema"]
            if call.args[1] == "delete_old_events":
                self.assertEqual(schema({"config_entry_id": "id"}), {
                    "config_entry_id": "id"
                })
                with self.assertRaises(vol.Invalid):
                    schema({})
                with self.assertRaises(vol.Invalid):
                    schema({"config_entry_id": "id", "date": "2026-11-18"})
                continue
            data = schema({
                "config_entry_id": "id", "date": "2026-11-18", "shift": "Night"
            })
            self.assertEqual(data["shift"], "Night")


class TestShiftServices(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = SimpleNamespace(
            calendar="calendar.shifts",
            async_create_shift=AsyncMock(),
            async_update_shift=AsyncMock(),
            async_delete_old_events=AsyncMock(),
        )
        self.user = MagicMock()
        self.hass = SimpleNamespace(
            data={"shift_planner": {"entry": self.coordinator}},
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            services=MagicMock(),
        )
        async_setup_services(self.hass)
        self.handler = self.hass.services.async_register.call_args.args[2]
        self.call = SimpleNamespace(
            service="update_shift",
            data={"config_entry_id": "entry", "date": "2026-11-18", "shift": "Late"},
            context=SimpleNamespace(user_id="user"),
        )

    async def test_services_dispatch_with_context(self):
        for service in ("create_shift", "update_shift"):
            self.call.service = service
            await self.handler(self.call)
            action = getattr(self.coordinator, f"async_{service}")
            action.assert_awaited_once_with(
                date(2026, 11, 18), "Late", self.call.context
            )

    async def test_cleanup_dispatches_with_only_entry_and_context(self):
        self.call.service = "delete_old_events"
        self.call.data = {"config_entry_id": "entry"}
        await self.handler(self.call)
        self.coordinator.async_delete_old_events.assert_awaited_once_with(
            self.call.context
        )
        self.coordinator.async_create_shift.assert_not_awaited()
        self.coordinator.async_update_shift.assert_not_awaited()

    async def test_cleanup_checks_permissions_and_loaded_entry(self):
        self.call.service = "delete_old_events"
        self.call.data = {"config_entry_id": "entry"}
        self.user.permissions.check_entity.return_value = False
        with self.assertRaises(Unauthorized):
            await self.handler(self.call)
        self.call.data["config_entry_id"] = "missing"
        with self.assertRaisesRegex(ServiceValidationError, "not loaded"):
            await self.handler(self.call)
        self.coordinator.async_delete_old_events.assert_not_awaited()

    async def test_update_rejects_unauthorized_user(self):
        for user in (None, self.user):
            with self.subTest(user=user):
                self.user.permissions.check_entity.return_value = False
                self.hass.auth.async_get_user.return_value = user
                with self.assertRaises(Unauthorized):
                    await self.handler(self.call)
        self.coordinator.async_update_shift.assert_not_awaited()

    async def test_update_rejects_unloaded_entry_or_invalid_date(self):
        self.call.data["date"] = "2026-02-30"
        with self.assertRaisesRegex(ServiceValidationError, "YYYY-MM-DD"):
            await self.handler(self.call)
        self.call.data["config_entry_id"] = "missing"
        with self.assertRaisesRegex(ServiceValidationError, "not loaded"):
            await self.handler(self.call)
        self.coordinator.async_update_shift.assert_not_awaited()
