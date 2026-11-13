"""Times shown to people: a 12-hour clock in the PC's own time zone (Pacific here), e.g.
"9:05 PM", "6:10 AM today" or "Nov 12 9:05:27 PM".

Display only. Stored data keeps Unix times and file names keep UTC.
"""

from __future__ import annotations

from datetime import datetime


def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch).astimezone()


def clock(epoch: float, *, seconds: bool = False) -> str:
    """'9:05 PM', or '9:05:27 PM' with seconds."""
    moment = _local(epoch)
    hour = moment.hour % 12 or 12
    tail = f":{moment.second:02d}" if seconds else ""
    return f"{hour}:{moment.minute:02d}{tail} {'AM' if moment.hour < 12 else 'PM'}"


def stamp(epoch: float, *, seconds: bool = True) -> str:
    """With the date: 'Nov 12 9:05:27 PM'."""
    moment = _local(epoch)
    return f"{moment:%b} {moment.day} {clock(epoch, seconds=seconds)}"


def when(epoch: float, now: float | None = None) -> str:
    """'6:10 AM today', '11:40 PM yesterday', '5:54 AM tomorrow' or 'Nov 10 4:03 AM'."""
    moment = _local(epoch)
    today = (datetime.now() if now is None else datetime.fromtimestamp(now)).date()
    days = (moment.date() - today).days
    word = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(days)
    return f"{clock(epoch)} {word}" if word else stamp(epoch, seconds=False)


def zone(epoch: float | None = None) -> str:
    """The time zone's short name, e.g. 'PST' (or 'PDT' in summer). Windows gives the
    long name ('Pacific Standard Time'), so that is shortened to its initials."""
    moment = datetime.now().astimezone() if epoch is None else _local(epoch)
    name = moment.tzname() or ""
    if name == "Coordinated Universal Time":
        return "UTC"
    if " " in name:
        return "".join(word[0] for word in name.split() if word[:1].isalpha()).upper()
    return name
