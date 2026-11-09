"""Identity derived only from AstrBot events, never caller-provided flags."""

from __future__ import annotations

from .models import Actor, NativeError


def _identifier(value: object, name: str, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise NativeError("identity", f"Invalid AstrBot {name}")
    result = str(value).strip()
    if required and not result:
        raise NativeError("identity", f"Missing AstrBot {name}")
    return result


def actor_from_event(event: object) -> Actor:
    """Use sender ID for user accounting; UMO remains the conversation address.

    Public API callers must pass a real AstrBot event. Python plugins already run
    inside the same trusted process; this is not an isolation boundary against a
    malicious plugin fabricating an event.
    """
    try:
        platform = _identifier(event.get_platform_id(), "platform_id")
        bot = _identifier(event.get_self_id(), "self_id")
        sender = _identifier(event.get_sender_id(), "sender_id")
        group = _identifier(event.get_group_id(), "group_id", required=False)
        umo = _identifier(event.unified_msg_origin, "unified_msg_origin")
        message_id = _identifier(getattr(event.message_obj, "message_id", ""), "message_id", required=False)
    except (AttributeError, TypeError) as exc:
        raise NativeError("identity", "A complete AstrBot message event is required") from exc
    # AstrBot 4.29.4 is_admin() is defined as role == "admin". Do not accept
    # arbitrary event.is_admin booleans or group/platform administrator flags.
    return Actor(platform, bot, sender, umo, group, getattr(event, "role", None) == "admin", message_id)
