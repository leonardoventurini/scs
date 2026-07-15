# SCS launchd services

`scs service install` generates user-agent property lists from the installed
SCS executable. The proxy owns the stable public MCP port and starts before the
daemon; the daemon owns the internal MCP port and SCSWire socket. Generated
property lists intentionally contain no persistent-data deletion behavior.

Use `scs service install|start|stop|restart|status|uninstall`; do not operate
the generated plists directly. Start order is proxy then daemon; stop order is
daemon then proxy. The proxy owns discovery and `proxy-service.json`; the
daemon owns `scs.sock` and `daemon-service.json`. Generation-checked cleanup
keeps independent restarts from deleting the survivor's artifacts. Uninstall
preserves `SCS_HOME`, indexes, provider metadata, and job history.
