"""SCS MCP Proxy — persistent front-door for the daemon's MCP endpoint.

Binds the public MCP port and forwards to the daemon's internal MCP port. When
the daemon is briefly unreachable (typical during hot-reload), queues incoming
requests for a bounded window and replays them once the upstream returns. See
``proxy.py`` for the forwarding logic and ``main.py`` for the CLI entry point.
"""

from .discovery import DiscoveryPublisher
from .proxy import ProxyConfig, ProxyServer, build_app

__all__ = ["DiscoveryPublisher", "ProxyConfig", "ProxyServer", "build_app"]
