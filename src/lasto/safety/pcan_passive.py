"""Passive (listen-only) PCAN channel: safety rule 1.

Listen-only mode is set on the channel before it is initialized, so the
controller never comes up in normal mode, and the setting is read back after
initialization. If either step fails, the channel is closed and capture
refuses to start. This module and everything it imports contain no transmit
call; tests/safety/test_structure.py enforces that.
"""

from __future__ import annotations

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import refuse
from lasto.safety.errors import InterfaceError, PassiveModeUnconfirmed
from lasto.safety.pcan_dll import PcanChannel, ReadOnlyPcan, check_available, check_driver, load_readonly


class PassiveChannel(PcanChannel):
    """A PCAN channel confirmed to be in hardware listen-only mode. It has no way to transmit."""


def open_passive(channel_name: str, *, pcan: ReadOnlyPcan | None = None) -> PassiveChannel:
    handle = pc.channel_handle(channel_name)
    pcan = load_readonly() if pcan is None else pcan
    api_version = check_driver(pcan)
    check_available(pcan, handle, channel_name)
    status = pcan.set_value(handle, pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_ON)
    if status != pc.PCAN_ERROR_OK:
        refuse(
            PassiveModeUnconfirmed(
                f"could not set listen-only on {channel_name} before initializing it: {pcan.error_text(status)}"
            ),
            transport="pcan",
            request=f"open {channel_name} listen-only",
            reason="listen_only_not_set",
        )
    status = pcan.initialize(handle, pc.PCAN_BAUD_500K)
    if status != pc.PCAN_ERROR_OK:
        raise InterfaceError(f"could not initialize {channel_name}: {pcan.error_text(status)}")
    channel = PassiveChannel(pcan, handle, channel_name, api_version)
    try:
        if not channel.listen_only():
            refuse(
                PassiveModeUnconfirmed(
                    f"{channel_name} did not read back as listen-only after initializing; refusing to capture"
                ),
                transport="pcan",
                request=f"open {channel_name} listen-only",
                reason="listen_only_not_confirmed",
            )
        channel.enable_reporting()
    except BaseException:
        channel.close()
        raise
    return channel
