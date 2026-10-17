# Running QSMO -- four terminals, always in this order

| # | Terminal | Command | Leave running? |
|---|----------|---------|----------------|
| 1 | Network    | `./run_network.sh`   | yes (type `pingall` here) |
| 2 | Controller | `./run_controller.sh`| yes |
| 3 | Monitor    | `./run_monitor.sh`   | yes |
| 4 | Migration  | `./run_migration.sh` | no -- run it as often as you like |

(1 and 2 may be started in either order; the switches keep retrying. Without the controller
`pingall` cannot work: switches are fail-secure.)

## 1. Network -- `./run_network.sh`
Topology comes from `network/topo.py` (or `./run_network.sh --topology ring:6`). Before starting Mininet it
makes sure the TLS certificates exist and are consistent, restarts Open vSwitch if it still holds an old CA,
and cleans the previous Mininet run (`mn -c`). `topo.py` then regenerates flows / ports / Hybrid certs for
the current topology and the controller learns it by LLDP.

## 2. Controller -- `./run_controller.sh`
OS-Ken with the topology adapter, the L2 switch and the OpenFlow echo probe. It publishes the live topology to
`runtime/topology.json` every 5 s and serves real echo-RTT probes to the monitor on `127.0.0.1:6691`.

## 3. Monitor -- `./run_monitor.sh`
Its own program. It waits for the controller, reconciles the saved state with OVS (Hybrid stays Hybrid; dead
tunnels are restarted), measures + saves Legacy/Hybrid baselines, watches every Hybrid switch and rolls back only on
repeated degradation. Ctrl+C stops only the monitor. Restart it any time: it picks the saved state up again.

## 4. Migration -- `./run_migration.sh`
    ./run_migration.sh                        # SGBM + BBM, continues from saved state (beta 0.5 = target 50% Hybrid)
    ./run_migration.sh migrate --beta 1.0     # go to 100%
    ./run_migration.sh migrate --switches s3,s5
    ./run_migration.sh status
    ./run_migration.sh rollback s3            # or --all
    ./run_migration.sh rebaseline
    ./run_migration.sh reset                  # clean slate: all Legacy, stunnel killed, state wiped
After a migration the new states are in the database, so the monitor immediately monitors the updated network and the
next migration run continues from it (already-Hybrid switches are not touched).

## Where state lives -- `<project>/runtime/`
`qsmo.db` (switches Legacy/Hybrid + port, ledger, baselines, events), `topology.json`, `monitor_status.json`.
It is in `.gitignore`, so some editors hide it: use `ls -a runtime` or `./run_migration.sh status`.

## Why you no longer kill stunnel
* stunnel runs in its own session: it survives Ctrl+C of every terminal above.
* A switch stays Hybrid until a rollback. Restart the controller / monitor / network or run the migration again:
  the saved state is reconciled with OVS and the tunnels are adopted (or restarted if dead).
* If OVS lost the Hybrid target (network rebuilt) it is re-applied; if the Hybrid path cannot be restored the switch
  goes back to Legacy instead of being left on a dead tunnel.

## Pingall fails / `SSLV3_ALERT_CERTIFICATE_UNKNOWN` in the controller log
The switches reject the controller's certificate. Run `./check_tls.sh` (controller + network up). Usual fix:
`QSMO_RESTART_OVS=1 ./run_network.sh` (makes Open vSwitch reload the current CA).

## First start on an already-messy testbed
    ./run_migration.sh reset --yes     # once
then follow the four terminals above.
