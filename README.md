# ha-shift-planner

![Shift Planner icon](custom_components/shift_planner/brand/icon.png)

Home Assistant integration to plan work shifts with a local calendar

Use the [Shift Planner card](https://github.com/mrunde/ha-shift-planner-card)
to display and manage shifts from a Home Assistant dashboard.

## Configure shifts

After adding the integration and selecting a writable Local Calendar, open
**Settings > Devices & services > Shift Planner > Configure** to add, edit, or
remove shifts. Each shift has a name, start time, and end time. An end time
before the start time means the shift ends on the following day; the start and
end times cannot be the same. Each calendar has its own shift configuration.

Existing installations retain the four original shifts and their 8 hour
30 minute duration until changed. Editing or removing a shift does not change
events already in the calendar.

The `shift_planner.create_shift` action takes the configured shift name in its
`shift` field. Available names and times are also exposed in the planner
sensor's `shifts` attribute. The action only creates a shift if the selected
day has no calendar events.
