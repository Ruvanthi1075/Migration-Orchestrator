# Quantum Safe Migration Orchestrator (QSMO)

Hybrid SDN controller migration with SGBM, BBM, health monitoring, and automatic recovery.

QSMO coordinates migration of SDN switches from **Legacy** mode to **Hybrid** mode in a Mininet/Open vSwitch environment. It verifies controller connections, monitors health, and rolls back automatically when health thresholds are violated.

> **Note:** Rollback demos use deliberately injected synthetic health values. They are test inputs, not production measurements.

## Requirements

Ubuntu with: Python 3, Mininet, Open vSwitch, OS-Ken, stunnel, OpenSSL, Git. Network emulation requires `sudo`.

## Setup (once)

```bash
git clone https://github.com/Ruvanthi1075/Migration-Orchestrator.git
cd Migration-Orchestrator
git checkout monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt   # if present
```

Point stunnel at your local clone:

```bash
sed -i 's#/home/ruvan/Migration-Orchestrator#'"$PWD"'#g' migration/hybrid/stunnel/server.conf
grep -n "/home/" migration/hybrid/stunnel/server.conf   # should only show your path
mkdir -p migration/hybrid/stunnel/logs
```

## Run

Open separate terminals and run **in this order**. Keep each one running. Run all from the repo root.

### Terminal 1 — Network

```bash
./run_network.sh
```

### Terminal 2 — Controller

```bash
./run_controller.sh
```

### Sync terminal — Flows, ports, Hybrid certificates

Run while the network and controller are up.

```bash
sudo chown -R $USER:$USER migration docs network reports 2>/dev/null
bash sync_topology.sh
ls migration/hybrid/certs/
```

### Terminal 3 — Monitor

```bash
./run_monitor.sh
```

### Terminal 4 — Migration

Default (budget `beta = 0.5`):

```bash
sudo ./run_migration.sh
```

Change the migration budget:

```bash
./run_migration.sh migrate --beta 0.7    # migrate more switches
./run_migration.sh migrate --beta 1.0    # migrate everything
```

Pick specific switches (**ignores the budget**):

```bash
./run_migration.sh migrate --switches s3,s5
```

## Expected output

The monitor shows per-switch state, controller target, connection, latency, and health. Migrated switches are `Hybrid` and connect to their per-switch stunnel port (`tcp:127.0.0.1:200xx`). Unmigrated switches stay `Legacy` on `ssl:127.0.0.1:6653` with no Hybrid latency.

```text
switch  dpid                state   ovs target            conn  legacy ms  hybrid ms  health
s0      0000000000000001    Hybrid  tcp:127.0.0.1:20001   yes   0.48       0.82       healthy
s1      0000000000000002    Hybrid  tcp:127.0.0.1:20002   yes   0.31       0.55       healthy
...
s11     000000000000000c    Legacy  ssl:127.0.0.1:6653    yes   0.65       -          -
s12     000000000000000d    Hybrid  tcp:127.0.0.1:20013   yes   0.80       1.37       healthy
...
s14     000000000000000f    Legacy  ssl:127.0.0.1:6653    yes   0.38       -          -
```

Quick controller check:

```bash
sudo ovs-vsctl list Controller | grep -E "target|is_connected"
```

## Cleanup (between runs)

```bash
sudo pkill -9 -f osken-manager; sudo pkill -9 stunnel
sudo mn -c
```

## Changing the topology

`network/topo.py` is the only file to edit. Coverage model, optimizer, monitor, certificates, ports, and flows adapt automatically.

```bash
sudo python3 network/topo.py --topology ring:6   # line:N | ring:N | star:N | mesh:N | tree:DEPTH:FANOUT
```

Then regenerate certificates and re-sync:

```bash
rm -rf migration/certs migration/hybrid/certs
./migration/gen_certs.sh
openssl verify -CAfile migration/certs/ca.cert migration/certs/controller.cert migration/certs/switch.cert
bash sync_topology.sh
```

Rules enforced before Mininet starts: unique non-zero 16-hex dpids, connected switch graph, hosts attached to existing switches. STP is enabled only when the topology has a loop.

Offline check (no Mininet): `python3 tests/test_any_topology.py`

## Tests

```bash
python3 -m pytest tests/
```

Integration tests need Mininet, OVS, OS-Ken, and `sudo`. A failure from a missing dependency is not a code failure.

## Console API

FastAPI backend (no authentication) for a management dashboard.

```bash
pip install -r console/requirements.txt
uvicorn console.main:app --port 8000
```

Open `/docs`. See `console/ENDPOINTS.md`.

## Repository layout

```text
Migration-Orchestrator/
├── controller/       # SDN controller app and switch behavior
├── network/          # topo.py, coverage model, flow generator
├── migration/        # Migration workflow, ports, certificates, monitoring
├── optimizer/        # SGBM optimizer, capability tracker, experiments
├── console/          # FastAPI management-console API
├── tests/            # Integration, monitoring, any-topology tests
├── sync_topology.sh  # Regenerates flows / ports / Hybrid certs
├── run_network.sh
├── run_controller.sh
├── run_monitor.sh
├── run_migration.sh
└── README.md
```

## Troubleshooting

- Run the cleanup commands above to clear stale `osken-manager`/`stunnel` processes.
- Confirm certificates exist in `migration/certs/` and `migration/hybrid/certs/`.
- Run commands from the repo root.
- Check the controller log for the first error before changing files.

## Limitations

- Demonstrated only in an emulated Mininet environment.
- Rollback demo uses synthetic health inputs.
- Results vary with software versions, system configuration, and privileges.
- Emulation results do not establish production performance, scalability, or security.

## Disclaimer

For research, development, and controlled demonstration. Validate configuration and behavior before using any component in a real network.
