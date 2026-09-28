"""The kill-switch hotkey, Ctrl+Alt+K (rule 7).

RegisterHotKey on a background thread that runs a Windows message loop, so
the hotkey works even when the terminal isn't focused. Pressing it trips the
process kill switch. A polled session shouldn't start if the hotkey can't be
registered.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from lasto.safety._frozen import SealedType, freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import InterfaceError
from lasto.safety.killswitch import KILL_SWITCH

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_K = 0x4B
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0x4C4B
START_TIMEOUT = 2.0


class HotkeyKillSwitch(metaclass=SealedType):
    __slots__ = ("_error", "_kernel32", "_ready", "_start_timeout", "_thread", "_thread_id", "_user32")

    def __init__(self, *, user32: object = None, kernel32: object = None, start_timeout: float = START_TIMEOUT) -> None:
        self._user32 = ctypes.windll.user32 if user32 is None else user32
        self._kernel32 = ctypes.windll.kernel32 if kernel32 is None else kernel32
        self._start_timeout = start_timeout
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lasto-kill-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._start_timeout):
            refuse(
                InterfaceError("the kill-switch hotkey thread didn't start"),
                transport="hotkey",
                request="register Ctrl+Alt+K",
                reason="hotkey_not_started",
            )
        if self._error is not None:
            refuse(
                InterfaceError(self._error), transport="hotkey", request="register Ctrl+Alt+K", reason="hotkey_not_registered"
            )

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(self._start_timeout)

    def _run(self) -> None:
        self._thread_id = self._kernel32.GetCurrentThreadId()
        if not self._user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_K):
            self._error = "could not register the Ctrl+Alt+K kill-switch hotkey; another program may be using it"
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        try:
            while self._user32.GetMessageW(ctypes.pointer(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    KILL_SWITCH.trip("hotkey")
        finally:
            self._user32.UnregisterHotKey(None, HOTKEY_ID)


freeze(__name__)
