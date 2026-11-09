"""Frozen entry point for the PyInstaller build created by build_binary.py.

PyInstaller runs this file as the program's main module, so the guard is what
makes it executable. The exit code comes straight from the CLI.
"""

from pressurecooker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
