# QSMO Port Assignments

This document records the TCP ports used by the Quantum-Safe Migration
Orchestrator Hybrid control-plane migration path.

## Switch-Side Hybrid Ports

Each migrated switch uses a unique local loopback port. The port is
derived from the last byte of the switch DPID:

`local_port = 20000 + last_byte_of_dpid`

| Switch | DPID | Local Hybrid Port | Purpose |
|---|---|---:|---|
| s1 | `0000000000000001` | `20001` | Switch-side Hybrid stunnel client |
| s2 | `0000000000000002` | `20002` | Switch-side Hybrid stunnel client |
| s3 | `0000000000000003` | `20003` | Switch-side Hybrid stunnel client |
| s4 | `0000000000000004` | `20004` | Switch-side Hybrid stunnel client |
| s5 | `0000000000000005` | `20005` | Switch-side Hybrid stunnel client |
| s6 | `0000000000000006` | `20006` | Switch-side Hybrid stunnel client |
| s0 | `0000000000000010` | `20016` | Switch-side Hybrid stunnel client |

## Shared Controller-Side Ports

| Port | Address | Purpose |
|---:|---|---|
| `16653` | `0.0.0.0:16653` | Shared Hybrid stunnel server; accepts PQC TLS connections from switch-side clients |
| `6660` | `127.0.0.1:6660` | Internal plaintext connection from Hybrid stunnel server to legacy rewrap service |
| `6653` | `127.0.0.1:6653` | OS-Ken OpenFlow control endpoint / Legacy TLS rewrap destination |

## Hybrid Connection Path

For a migrated switch:

```text
OVS
 |
 | tcp:127.0.0.1:<local_port>
 v
Switch-side stunnel client
 |
 | TLS 1.3
 | X25519MLKEM768
 v
Controller-side stunnel server
 |
 | plaintext loopback
 | 127.0.0.1:6660
 v
Legacy rewrap stunnel
 |
 | classical TLS
 | 127.0.0.1:6653
 v
OS-Ken
