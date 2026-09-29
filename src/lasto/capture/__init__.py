"""Capture: turns what a channel reads into stored data. It runs inside lasto.operations.

The one place outside the safety core that uses the core's frame types, converting them to plain
records (lasto.records) for everything downstream. Nothing hardware-free (services, storage, and
later the GUI) imports capture (tests/test_layers.py).
"""
