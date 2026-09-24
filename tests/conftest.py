"""Shared fixtures and a lightweight stand-in for Langflow.

Langflow is a heavy dependency and the parsers use only a handful of its
classes, so when it is not installed the tests run against minimal stubs that
mimic the relevant behaviour. With Langflow installed, the real classes are used.
"""

import base64
import sys
import types
from io import BytesIO

import pytest
from PIL import Image


def _install_langflow_stubs() -> None:
    class Data:
        """Mimics langflow.schema.Data: extra kwargs go into .data, attributes read from it."""

        def __init__(self, data=None, **kwargs):
            self.data = {**(data or {}), **kwargs}

        def __getattr__(self, key):
            if key.startswith('_') or key == 'data':
                raise AttributeError(key)
            try:
                return self.data[key]
            except KeyError:
                raise AttributeError(key) from None

    class Input:
        def __init__(self, *, name, value=None, **kwargs):
            self.name = name
            self.value = value
            self.__dict__.update(kwargs)

    class Output:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Component:
        """Mimics how Langflow exposes input values as instance attributes."""

        inputs: list = []
        outputs: list = []

        def __init__(self, **kwargs):
            for inp in self.inputs:
                setattr(self, inp.name, inp.value)
            for key, value in kwargs.items():
                setattr(self, key, value)

        def log(self, message):
            pass

    modules = {
        'langflow': types.ModuleType('langflow'),
        'langflow.custom': types.ModuleType('langflow.custom'),
        'langflow.io': types.ModuleType('langflow.io'),
        'langflow.schema': types.ModuleType('langflow.schema'),
    }
    modules['langflow.custom'].Component = Component
    modules['langflow.schema'].Data = Data
    io = modules['langflow.io']
    io.Output = Output
    for name in ('DataInput', 'IntInput', 'BoolInput', 'DropdownInput'):
        setattr(io, name, type(name, (Input,), {}))
    sys.modules.update(modules)


try:
    import langflow  # noqa: F401
except ImportError:
    _install_langflow_stubs()


# ---------- helpers ----------


def make_png(width=120, height=80, color=(200, 30, 30), mode='RGB') -> bytes:
    buf = BytesIO()
    Image.new(mode, (width, height), color).save(buf, format='PNG')
    return buf.getvalue()


def decode_b64(b64: str) -> Image.Image:
    im = Image.open(BytesIO(base64.b64decode(b64)))
    im.load()
    return im


@pytest.fixture
def warnings_of():
    """Capture the component's warnings: `messages = warnings_of(parser)`."""

    def attach(component):
        messages: list[str] = []
        component._warn = messages.append
        return messages

    return attach
