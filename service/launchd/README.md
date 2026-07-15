# SCS launchd services

`scs service install` generates user-agent property lists from the installed
SCS executable. The proxy owns the stable public MCP port and starts before the
daemon; the daemon owns the internal MCP port and SCSWire socket. Generated
property lists intentionally contain no persistent-data deletion behavior.
