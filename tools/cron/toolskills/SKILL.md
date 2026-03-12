# cron

Schedule reminders and recurring tasks. Actions: add, list, remove.

## Parameters
- `action`: Action to perform
- `message`: Reminder message (for add)
- `every_seconds`: Interval in seconds (for recurring tasks)
- `cron_expr`: Cron expression like '0 9 * * *' (for scheduled tasks)
- `tz`: IANA timezone for cron expressions (e.g. 'America/Vancouver')
- `at`: ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')
- `job_id`: Job ID (for remove)

## Usage
Use `cron` only when it is the most direct way to complete the task.
