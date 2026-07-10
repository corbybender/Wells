"""Entry point for ``python -m wells`` — starts the MCP server.

This makes ``python -m wells`` behave identically to
``wells-mcp``, which is convenient for MCP client configurations
that accept a ``python -m`` module path.
"""

from wells.mcp_server import main

main()
