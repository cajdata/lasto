"""Storage: the data folder, the SQLite databases, and (later in Phase 2) the raw segment files.

Hardware-free and below services: capture writes through it, services read through it, and it
reaches neither, nor the safety core (tests/test_layers.py). The capture database is written only
by capture processes; the workbench database holds what the GUI and the CLI's analysis write, so
the two never contend for one writer (docs/architecture.md §14.3).
"""
