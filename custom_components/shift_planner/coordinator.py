import asyncio
from datetime import date, datetime, time, timedelta
import logging
from typing import Any, TypedDict

from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_CALENDAR, CONF_SHIFTS, DEFAULT_SHIFTS, DOMAIN

_LOGGER = logging.getLogger(__name__)


class DayData(TypedDict):
    date: str
    summaries: list[str]


class PlannerData(TypedDict):
    week_start: str
    days: list[DayData]


def monday() -> date:
    today = dt_util.now().date()
    return today - timedelta(days=today.weekday())


def midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=dt_util.DEFAULT_TIME_ZONE)


def event_time(value: str) -> datetime:
    if len(value) == 10:
        return midnight(date.fromisoformat(value))

    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid calendar timestamp: {value}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    return dt_util.as_local(parsed)


class ShiftCoordinator(DataUpdateCoordinator[PlannerData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=5),
        )
        self.entry = entry
        self.lock = asyncio.Lock()

    @property
    def calendar(self) -> str:
        registered = (
            er.async_get(self.hass).async_get(self.entry.unique_id)
            if self.entry.unique_id
            else None
        )
        return (
            registered.entity_id
            if registered is not None
            else self.entry.data[CONF_CALENDAR]
        )

    @property
    def shifts(self) -> dict[str, dict[str, str]]:
        shifts = self.entry.options.get(CONF_SHIFTS, DEFAULT_SHIFTS)
        return dict(
            sorted(
                shifts.items(),
                key=lambda item: time.fromisoformat(item[1]["start"]),
            )
        )

    async def _events(
        self,
        start: datetime,
        end: datetime,
        context: Context | None = None,
    ) -> list[dict[str, Any]]:
        state = self.hass.states.get(self.calendar)
        if state is None or state.state in ("unknown", "unavailable"):
            raise HomeAssistantError("The calendar is unavailable")

        response = await self.hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": self.calendar,
                "start_date_time": start.isoformat(),
                "end_date_time": end.isoformat(),
            },
            blocking=True,
            return_response=True,
            context=context,
        )

        if response is None or self.calendar not in response:
            raise HomeAssistantError("The calendar returned no response")

        return response[self.calendar]["events"]

    async def _async_update_data(self) -> PlannerData:
        try:
            async with self.lock:
                first = monday()
                events = await self._events(
                    midnight(first),
                    midnight(first + timedelta(days=14)),
                )

                intervals = [
                    (
                        event_time(event["start"]),
                        event_time(event["end"]),
                        event["summary"],
                    )
                    for event in events
                ]
                intervals.sort(key=lambda interval: interval[0])

                days: list[DayData] = []

                for offset in range(14):
                    day = first + timedelta(days=offset)
                    start = midnight(day)
                    end = midnight(day + timedelta(days=1))

                    days.append({
                        "date": day.isoformat(),
                        "summaries": [
                            title
                            for begin, finish, title in intervals
                            if begin < end and finish > start
                        ],
                    })

                return {
                    "week_start": first.isoformat(),
                    "days": days,
                }

        except (HomeAssistantError, ValueError, KeyError, TypeError) as err:
            raise UpdateFailed(
                f"Cannot read the shift calendar: {err}"
            ) from err

    def _shift_times(self, day: date, shift: str) -> tuple[datetime, datetime]:
        first = monday()
        shift_config = self.shifts.get(shift)
        if shift_config is None:
            raise ServiceValidationError(f"Unknown shift: {shift}")

        if not first <= day < first + timedelta(days=14):
            raise ServiceValidationError(
                "The date must be in the current week or next week"
            )

        start = datetime.combine(
            day,
            time.fromisoformat(shift_config["start"]),
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        )
        end = datetime.combine(
            day,
            time.fromisoformat(shift_config["end"]),
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        )
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def _calendar_entity(
        self, feature: CalendarEntityFeature, action: str
    ) -> CalendarEntity:
        state = self.hass.states.get(self.calendar)
        if state is None or state.state in ("unknown", "unavailable"):
            raise HomeAssistantError("The calendar is unavailable")

        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(self.calendar) if component else None
        if entity is None:
            raise HomeAssistantError("The calendar is not loaded")
        if not (entity.supported_features and entity.supported_features & feature):
            raise ServiceValidationError(
                f"The calendar does not support {action} events"
            )
        return entity

    async def async_create_shift(
        self,
        day: date,
        shift: str,
        context: Context,
    ) -> None:
        async with self.lock:
            start, end = self._shift_times(day, shift)

            events = await self._events(
                midnight(day),
                midnight(day + timedelta(days=1)),
                context,
            )

            if events:
                raise ServiceValidationError(
                    f"There is already an event on {day.isoformat()}"
                )

            await self.hass.services.async_call(
                "calendar",
                "create_event",
                {
                    "entity_id": self.calendar,
                    "summary": shift,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                blocking=True,
                context=context,
            )

        await self.async_request_refresh()

    async def async_update_shift(
        self,
        day: date,
        shift: str,
        context: Context,
    ) -> None:
        async with self.lock:
            start, end = self._shift_times(day, shift)
            entity = self._calendar_entity(
                CalendarEntityFeature.UPDATE_EVENT, "updating"
            )

            # The get_events action omits the UID needed to update an event.
            events = await entity.async_get_events(
                self.hass, midnight(day), midnight(day + timedelta(days=1))
            )
            if len(events) != 1:
                raise ServiceValidationError(
                    "The selected day must have exactly one calendar event"
                )

            event = events[0]
            if event.start_datetime_local.date() != day:
                raise ServiceValidationError(
                    "Select the date on which the event starts"
                )
            if not event.uid:
                raise ServiceValidationError("The calendar event has no UID")

            entity.async_set_context(context)
            try:
                await entity.async_update_event(
                    event.uid,
                    {
                        "summary": shift,
                        "dtstart": start,
                        "dtend": end,
                        "description": event.description,
                        "location": event.location,
                    },
                    recurrence_id=event.recurrence_id,
                )
            except ValueError as err:
                raise ServiceValidationError(
                    f"Cannot update the calendar event: {err}"
                ) from err

        await self.async_request_refresh()

    async def async_delete_old_events(self, context: Context) -> None:
        deleted = 0
        try:
            async with self.lock:
                entity = self._calendar_entity(
                    CalendarEntityFeature.DELETE_EVENT, "deleting"
                )
                cutoff = midnight(monday())
                start = midnight(date.min)
                # Avoid UTC underflow in timezones east of Greenwich.
                if (offset := start.utcoffset()) is not None and offset > timedelta(0):
                    start += offset
                events = await entity.async_get_events(self.hass, start, cutoff)
                targets: list[tuple[str, str | None]] = []
                for event in events:
                    if event.start_datetime_local >= cutoff:
                        continue
                    if not event.uid:
                        raise ServiceValidationError("A calendar event has no UID")
                    if event.rrule and not event.recurrence_id:
                        raise ServiceValidationError(
                            "A recurring calendar event has no recurrence ID; "
                            "cannot safely preserve current and future occurrences"
                        )
                    targets.append((event.uid, event.recurrence_id))

                entity.async_set_context(context)
                for uid, recurrence_id in targets:
                    try:
                        await entity.async_delete_event(
                            uid, recurrence_id=recurrence_id
                        )
                    except (HomeAssistantError, ValueError) as err:
                        raise HomeAssistantError(
                            "Calendar cleanup stopped after deleting "
                            f"{deleted} events: {err}"
                        ) from err
                    deleted += 1
        finally:
            if deleted:
                await self.async_request_refresh()