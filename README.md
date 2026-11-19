# ha-shift-planner

![Shift Planner icon](custom_components/shift_planner/brand/icon.png)

Home Assistant integration to plan work shifts with a local calendar

Use the [Shift Planner card](https://github.com/mrunde/ha-shift-planner-card)
to display and manage shifts from a Home Assistant dashboard.

## Configure shifts

After adding the integration and selecting a writable Local Calendar, open
**Settings > Devices & services > Shift Planner > Configure** to see how many
shifts are configured and choose **Add shift**, **Edit shift**, or **Remove
shift**. When editing or removing a shift, the picker shows its name and hours.
Each shift has a name, start time, and end time. An end time
before the start time means the shift ends on the following day; the start and
end times cannot be the same. Each calendar has its own shift configuration.

Existing installations retain the four original shifts and their 8 hour
30 minute duration until changed. Editing or removing a shift does not change
events already in the calendar.

The `shift_planner.create_shift` action takes the configured shift name in its
`shift` field. Available names and times are also exposed in the planner
sensor's `shifts` attribute. The action only creates a shift if the selected
day has no calendar events.

## Update existing events

To change an existing event, use the `shift_planner.update_shift` action with
the same fields: `config_entry_id`, `date`, and `shift`. For example, choose
`Late` to change an early shift's name and start/end times to the configured
late shift. The selected date must be in the current week or next week, have
exactly one calendar event, and be the date on which that event starts.
Overnight shifts still end the next day. Changing shift configuration alone does
not modify existing calendar events.

```yaml
action: shift_planner.update_shift
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  date: "2026-11-18"
  shift: Late
```

## Delete old calendar events

Use `shift_planner.delete_old_events` to permanently delete **all events**
in the selected planner's calendar whose start is before this Monday at 00:00
in Home Assistant's timezone. This includes non-shift events and overnight
events starting last week even if they end this week. Events starting this
week (including Monday at exactly 00:00) and all future events remain.
Only past occurrences of recurring events are deleted, not the whole series.
Cleanup runs only when this action is called; it is not automatic.

```yaml
action: shift_planner.delete_old_events
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
```
