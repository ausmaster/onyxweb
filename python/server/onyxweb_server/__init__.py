"""onyxweb-server: serve onyxweb's browser to agents over MCP and to programs over HTTP.

``onyxweb_server.core`` holds the policy every front-end shares, ``onyxweb_server.egress`` the proxy
that keeps the browser off private addresses, and ``onyxweb_server.mcp`` and ``onyxweb_server.http``
the front-ends. Import them by full module path.
"""
