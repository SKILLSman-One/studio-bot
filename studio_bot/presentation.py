"""Shared labels for the studio's friendly Telegram interface."""


def room_name(room):
    return f"{room}-я комната" if str(room).isdigit() else str(room)


def button_label(label, action):
    if label in {"←", "→"}:
        return label
    name = action.split(":", 1)[0]
    icons = {
        "home": "🏡",
        "rooms": "🚪",
        "room": "✨",
        "end": "🌙",
        "week": "🗓",
        "day": "📅",
        "mybookings": "📋",
        "cancelbook": "✖️",
        "salary": "💵",
        "salarydates": "🔄",
        "customperiod": "📅",
        "advance": "💌",
        "myadvances": "🧾",
        "contact": "💬",
        "register": "🌸",
        "people": "👥",
        "bookings": "📋",
        "calendar": "🗓",
        "calendarview": "🗓",
        "advances": "💌",
        "tickets": "💬",
        "health": "🩺",
        "delivery": "🔔",
        "logout": "🚪",
        "bindstart": "🔗",
        "bindconfirm": "✅",
        "approveprofile": "✅",
        "rejectperson": "✖️",
        "usage": "⏱",
        "bookroom": "🚪",
        "bookconfirm": "✅",
        "payday": "💵",
        "cleanconfirm": "✅",
    }
    icon = icons.get(name)
    if name == "accesslist":
        icon = "🔓" if ":unban:" in action else "🔒"
    if not icon:
        return label
    return f"{icon} {label}"
