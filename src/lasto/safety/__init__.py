"""lasto safety core: the only code that can put bytes on a wire.

The rules this package enforces are in CLAUDE.md ("Safety core"), and how it
enforces them is in docs/architecture.md §3. Import the submodules you need
(for example lasto.safety.session and lasto.safety.requests). This file
imports only the freezing helper and the serial guard, which installs the
process's audit hook against opening a serial port anywhere but the STN
adapter link (lasto.safety.serial_guard), so importing the passive capture
path never loads the transmit binding. Like every safety module, the package
is frozen: nothing in it can be rebound at runtime (lasto.safety._frozen).
"""

from lasto.safety import serial_guard  # noqa: F401  (installs the audit hook)
from lasto.safety._frozen import freeze

freeze(__name__)
