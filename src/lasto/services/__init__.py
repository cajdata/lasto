"""Hardware-free application logic: what lasto does with the data, for every front end.

The CLI calls it, and from Phase 9 so does the GUI, so no logic lives only in one front end.
Services return plain data, stored SI; each front end renders it. Nothing here reaches the safety
core, the simulator, the hardware-facing operations, capture, or a hardware library
(tests/test_layers.py, docs/architecture.md §14.4).
"""
