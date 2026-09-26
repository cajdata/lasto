"""Command-line entry point for lasto."""

from lasto import __version__


def main() -> int:
    print(f"lasto {__version__}")
    print("lasto is pre-alpha and does not do anything yet. See https://lasto.dev")
    return 0
