"""PyInstaller entry point for the standalone `wells` binary.

PyInstaller needs a real script (not a console-script shim), so this is the
thinnest possible wrapper around the same callable the pip entry point uses.
"""

from wells.main import main

if __name__ == "__main__":
    main()
