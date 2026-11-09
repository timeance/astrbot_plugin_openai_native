from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image


class FakeEvent:
    def __init__(self, sender="user1", group="group1", message="m1", *, admin=False, components=None):
        self.sender, self.group = sender, group
        self.role = "admin" if admin else "member"
        self.unified_msg_origin = (
            "napcat:GroupMessage:" + group if group else "napcat:FriendMessage:" + sender
        )
        self.message_obj = SimpleNamespace(message_id=message)
        self.components = components or []

    def get_platform_id(self):
        return "napcat"

    def get_self_id(self):
        return "bot1"

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.components


@pytest.fixture
def event_factory():
    return FakeEvent


@pytest.fixture
def png_data():
    stream = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def gif_data():
    stream = BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    frames[0].save(stream, format="GIF", save_all=True, append_images=frames[1:], duration=30, loop=0)
    return stream.getvalue()
