import builtins
import io

import pytest

from core.progress_display import NoopProgressDisplay, RichProgressDisplay


class _TTY(io.StringIO):
    def isatty(self):
        return True


class _Console:
    def __init__(self):
        self.messages = []

    def print(self, message, **_kwargs):
        self.messages.append(message)


class _Live:
    is_started = True

    def __init__(self):
        self.events = []

    def stop(self):
        self.events.append("stop")
        self.is_started = False

    def start(self, refresh=False):
        self.events.append(("start", refresh))
        self.is_started = True


def _tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setattr("sys.stdout", _TTY())


def test_live_prompt_suspends_display_and_restores_it(monkeypatch):
    _tty(monkeypatch)
    display = RichProgressDisplay("run", "full")
    display._live = _Live()
    display._console = _Console()
    monkeypatch.setattr(builtins, "input", lambda: "APPROVE 1 LEGACY PERMISSIONS")

    answer = display.prompt("Type exact approval: ")

    assert answer == "APPROVE 1 LEGACY PERMISSIONS"
    assert display._live.events == ["stop", ("start", True)]
    assert display._live.is_started
    assert display._console.messages == ["Type exact approval: "]


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt()])
def test_live_prompt_restores_display_after_eof_or_interrupt(monkeypatch, exception):
    _tty(monkeypatch)
    display = RichProgressDisplay("run", "full")
    display._live = _Live()
    display._console = _Console()

    def abort():
        raise exception

    monkeypatch.setattr(builtins, "input", abort)
    with pytest.raises(type(exception)):
        display.prompt("Type exact approval: ")
    assert display._live.events == ["stop", ("start", True)]
    assert display._live.is_started


def test_noop_prompt_supports_no_live_ui_and_rejects_noninteractive(monkeypatch):
    _tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda prompt: "typed")
    assert NoopProgressDisplay().prompt("approval: ") == "typed"

    class Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", Pipe())
    with pytest.raises(RuntimeError, match="requires an interactive terminal"):
        NoopProgressDisplay().prompt("approval: ")
