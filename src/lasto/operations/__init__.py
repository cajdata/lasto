"""Hardware-facing work: capturing a drive, and later polling, snapshots, identify, and discovery.

The only code outside the safety core that opens its sessions (tests/test_layers.py). The CLI calls
it. The GUI never imports it; the one hardware action the GUI has, a passive capture, runs as a
separate `lasto drive` process (docs/architecture.md §14.1 and §14.4).
"""
