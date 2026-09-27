"""lasto safety core: the only code that can put bytes on a wire.

The rules this package enforces are in CLAUDE.md ("Safety core"), and how it
enforces them is in docs/architecture.md §3. Import the submodules you need
(for example lasto.safety.session and lasto.safety.requests). This file
imports nothing, so importing the passive capture path never loads the
transmit binding.
"""
