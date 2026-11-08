"""Current time and date tool for the KIVO agent runtime."""
from __future__ import annotations

import datetime
import time
from typing import Any

from kivo.agent.tool_schema import PermissionLevel, ToolDefinition


def handle_get_current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return local time, date, day of week, and timezone."""
    now = datetime.datetime.now().astimezone()
    tz_name = now.tzname() or time.tzname[0]

    return {
        "time": now.strftime("%I:%M:%S %p"),
        "date": now.strftime("%A, %B %d, %Y"),
        "day_of_week": now.strftime("%A"),
        "timezone": tz_name,
        "iso_timestamp": now.isoformat(),
    }


CURRENT_TIME_TOOL = ToolDefinition(
    name="get_current_time",
    description="Get the current local time, date, day of the week, and timezone from the operating system.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    handler=handle_get_current_time,
    permission=PermissionLevel.READ_ONLY,
    timeout_seconds=3.0,
)
