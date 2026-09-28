"""Rule 7: the Ctrl+Alt+K kill-switch hotkey, against fake user32/kernel32."""

import queue
import threading

import pytest

from lasto.safety.errors import InterfaceError
from lasto.safety.hotkey import HOTKEY_ID, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_K, WM_HOTKEY, WM_QUIT, HotkeyKillSwitch
from lasto.safety.killswitch import KillSwitch


class FakeUser32:
    def __init__(self, *, register_ok=True, hold_register=None):
        self.messages = queue.Queue()
        self.registered = []
        self.unregistered = []
        self.register_ok = register_ok
        self.hold_register = hold_register

    def RegisterHotKey(self, hwnd, ident, modifiers, key):
        if self.hold_register is not None:
            self.hold_register.wait()
        self.registered.append((ident, modifiers, key))
        return 1 if self.register_ok else 0

    def UnregisterHotKey(self, hwnd, ident):
        self.unregistered.append(ident)
        return 1

    def GetMessageW(self, message, hwnd, low, high):
        kind, wparam = self.messages.get(timeout=5)
        if kind == WM_QUIT:
            return 0
        message.contents.message = kind
        message.contents.wParam = wparam
        return 1

    def PostThreadMessageW(self, thread_id, kind, wparam, lparam):
        self.messages.put((kind, wparam))
        return 1


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 4242


def test_the_hotkey_trips_the_kill_switch():
    killswitch = KillSwitch()
    tripped = threading.Event()
    killswitch.add_listener(lambda cause: tripped.set())
    user32 = FakeUser32()
    listener = HotkeyKillSwitch(killswitch, user32=user32, kernel32=FakeKernel32())
    listener.start()
    assert user32.registered == [(HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_K)]
    user32.messages.put((WM_HOTKEY, HOTKEY_ID + 1))  # someone else's hotkey
    user32.messages.put((0x0100, HOTKEY_ID))  # not a hotkey message
    user32.messages.put((WM_HOTKEY, HOTKEY_ID))
    assert tripped.wait(5)
    assert killswitch.cause == "hotkey"
    listener.stop()
    listener.stop()  # stopping twice is harmless
    assert user32.unregistered == [HOTKEY_ID]


def test_stop_before_start_is_harmless():
    HotkeyKillSwitch(KillSwitch(), user32=FakeUser32(), kernel32=FakeKernel32()).stop()


def test_refuses_when_the_hotkey_is_taken():
    user32 = FakeUser32(register_ok=False)
    listener = HotkeyKillSwitch(KillSwitch(), user32=user32, kernel32=FakeKernel32())
    with pytest.raises(InterfaceError, match="Ctrl\\+Alt\\+K"):
        listener.start()
    assert user32.unregistered == []


def test_refuses_when_the_thread_does_not_start():
    hold = threading.Event()
    user32 = FakeUser32(hold_register=hold)
    listener = HotkeyKillSwitch(KillSwitch(), user32=user32, kernel32=FakeKernel32(), start_timeout=0.05)
    with pytest.raises(InterfaceError, match="didn't start"):
        listener.start()
    hold.set()
    listener.stop()


def test_defaults_to_the_real_windows_libraries():
    listener = HotkeyKillSwitch(KillSwitch())
    assert listener._user32 is not None and listener._kernel32 is not None
