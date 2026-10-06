"""Frozen test fixture, not the app: what sitegen reads from src/lasto/safety/hotkey.py, copied from
the app at dab6e21 (Phase 2 as approved), and nothing else: the key constants, and the one
RegisterHotKey call with the app's arguments (in the app it's a method of the hotkey thread).
See site/tests/fixtures/README.md.
"""

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_K = 0x4B
HOTKEY_ID = 0x4C4B


def _register(self):
    return self._user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_K)
