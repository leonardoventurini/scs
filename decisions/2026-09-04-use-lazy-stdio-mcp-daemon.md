# Use a lazy shared daemon behind per-harness stdio MCP bridges

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

Date: 2026-09-04

## Context

SCS used an always-on HTTP proxy and an internal HTTP MCP server, both managed
as launchd agents. This reserved two TCP ports, made installation macOS-only,
and required generation coordination between three transports even though the
daemon already exposed its complete service contract through SCSWire.

Agent harnesses naturally manage an MCP stdio child process. Multiple harnesses
must share one storage-owning daemon, and an abruptly killed bridge must not
leave permanent lifecycle state.

## Decision

Every harness launches a thin `scs mcp` stdio bridge. The bridge lazily starts
one shared daemon under a bootstrap lock and attaches a persistent SCSWire
connection. That connection is the client lease. Multiple bridges share the
daemon generation; after the final lease disconnects, a short handoff debounce
precedes orderly daemon shutdown.

The daemon exposes SCSWire only. The HTTP proxy, internal HTTP server, fixed TCP
ports, launchd definitions, and service-install commands are removed. Portable
`scs daemon start|stop|restart|status` commands replace platform lifecycle
management. Persistent data remains independent of process lifecycle.

## Rejected alternatives

- Keep launchd on macOS and add systemd on Linux. This doubles service-manager
  integration while retaining always-on processes.
- Let every MCP process own its own graph. This violates single-writer storage
  and duplicates memory-heavy indexes.
- Retain the internal HTTP MCP server. It adds a port and transport hop without
  providing value to local stdio harnesses.
- Persist lease tokens and require explicit release RPCs. Killed clients can
  orphan persisted leases; socket ownership gives kernel-managed cleanup.
- Stop the daemon with the first disconnected bridge. Concurrent harnesses
  would break each other.

## Consequences

- Installation is the same on supported macOS and Linux hosts and needs no
  privileged service registration.
- Background watchers and jobs live only while at least one MCP bridge remains;
  shutdown persists retryable job state before exit.
- The first request pays bounded daemon startup latency; subsequent bridges
  attach to the existing generation.
- Harness configuration points to an executable command instead of an HTTP URL.
- Old launchd installations must be stopped/uninstalled with the previous SCS
  binary before upgrading, or removed manually as documented for prerelease
  users.
