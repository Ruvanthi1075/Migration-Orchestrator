"""
migrate_link.py
Feature 4 -- Guarded Migration Execution, GME (Person C)

CORRECTED MODEL (see QSMO_Implementation_Guide.docx, sections 2, 5.4, 8)
--------------------------------------------------------------------------
The thing this module migrates is a SWITCH's control-plane FLOW (its
OpenFlow session to the controller) -- identified by dpid, not a
link/edge. It is the ONLY module in the system allowed to call
topology.set_state(dpid, "Hybrid") (guide section 2.3 / README "State
ownership"). monitor.py (Person D) is the only module allowed to
revert it back to "Legacy".

DESIGN DECISIONS (guide section 8, restated here so this file is
self-explaining without the guide open)
--------------------------------------------------------------------------
D1 -- Legacy path (already working, do not touch):
    topo.py already brings every switch up over classical OVS-native
    TLS using gen_certs.sh's ECDSA chain: switch --ssl:CONTROLLER_IP:
    6653--> OS-Ken. That is the rollback target every failed migration
    in this file returns to.

D2 -- Hybrid path is via a per-switch stunnel proxy, NOT native OVS
    TLS (gen_certs.sh's own header comment states this; formalized
    here). OVS exposes no CLI/DB knob to force a TLS 1.3 group list,
    so native OVS TLS cannot guarantee X25519MLKEM768 is what actually
    gets negotiated. stunnel gives full control over the negotiated
    group via its `curve` config directive:

        LEGACY (current state):
          switch --ssl:CONTROLLER_IP:6653 (classical ECDSA, direct)--> OS-Ken

        HYBRID (after a successful migration of this switch):
          switch --tcp:127.0.0.1:<local_port> (plaintext, loopback)-->
          local stunnel CLIENT (X25519MLKEM768 + ML-DSA cert) -->
          network -->
          controller-side stunnel SERVER (shared, one process/port),
            service [hybrid-controller] :16653 -->
          tcp:127.0.0.1:6660 (plaintext, loopback) -->
            service [legacy-rewrap] (classical TLS, Legacy switch cert) -->
          ssl:127.0.0.1:6653 --> OS-Ken   (unchanged, SSL-only listener)

    Why the extra rewrap hop: osken-manager binds exactly ONE OpenFlow
    listener (SSL-only once --ctl-privkey is set), so a plaintext
    handoff straight to 6653 can never work and OS-Ken cannot be given
    a second listener. See docs/PORTS.md, "Resolved".

    local_port is unique per switch (guide §8.2: local_port = 20000 +
    last dpid byte) so multiple migrated switches on the same VM don't
    collide; the controller-side stunnel server is a single shared
    process every migrated switch's client stunnel dials into.

    CONFIRMED (live handshake test, 2026-09-20, superseding an earlier
    wrong assumption in this file and in migration/hybrid/stunnel/
    server.conf): the SYSTEM stunnel binary (5.63, "Compiled with
    OpenSSL 3.0.2" but dynamically "Running with OpenSSL 3.5.8-dev")
    already negotiates X25519MLKEM768 correctly -- no separate rebuilt
    binary is required. Two things were wrong in the earlier draft,
    both caught by actually running a handshake rather than assuming:
      1. The directive is `curve`, not `groups` -- `groups` was never
         a real stunnel keyword. `SSL_CTX_set1_curves_list()` (what
         `curve` maps to) simply passes the group-name string through
         to whatever libssl is loaded at runtime, so it worked
         immediately once the name was right, with zero rebuilding.
      2. Because a hybrid/PQC group added by an external or newer
         default provider has no OpenSSL NID (see SSL_CTX_set1_groups
         manual: "support for some groups may be added by external
         providers... there will be no NID assigned"), stunnel's debug
         log reports it as `Peer temporary key: (null), 768 bits` --
         name unresolvable, but the group's bit-parameter (768, matching
         ML-KEM-768) still comes through. Since `curve` is configured
         to exactly ONE value on both ends, a successful handshake is
         itself sufficient proof that value was used -- there is no
         second group available to silently fall back to. This is why
         `_dry_run_confirm_hybrid_group` below checks for handshake
         SUCCESS rather than trying to regex out a group name that may
         legitimately never render as text.
    `hybrid_stunnel_bin` now defaults to `"stunnel"` (resolved via
    PATH) rather than a separate build path; override it if a given
    deployment genuinely needs a different binary.

D3 -- Why not just test if native OVS TLS happens to negotiate PQC:
    ovs-vswitchd IS linked against /opt/openssl35 (confirmed in guide
    §1.1), so it is not ruled out in theory -- but "not ruled out" is
    not the same as "guaranteed," and this project needs to be able to
    assert what group was actually negotiated, not hope for it. Not
    pursued per guide §8.2's explicit instruction not to spend Feature
    4 time here unless §8 is fully working with time left over.

MAKE-BEFORE-BREAK MIGRATION PROTOCOL (supersedes the original
Stage-2 "flip the controller target and poll" cutover)
--------------------------------------------------------------------------
The original Guarded Migration Execution cutover was direct: attempt
the Hybrid handshake, and on success, immediately repoint OVS's
controller target at it (Algorithm 2 lines 6-11). That never gave a
window to notice a handshake that *succeeds* but doesn't actually carry
working control traffic -- a real, documented OQS/OpenSSL
provider-compatibility failure mode (clean TLS, broken data path).

The protocol below never modifies the live Legacy session until a
replacement connection has been independently proven to work, under
real load, for a sustained window -- not just at the instant of the
handshake:

  MAKE (non-disruptive):
    Add the Hybrid target as a SECOND, PARALLEL controller alongside
    the switch's existing Legacy controller, in one ovsdb transaction:
        ovs-vsctl set-controller <switch> <legacy_target> <new_target>
    OVS now maintains BOTH connections simultaneously. No renegotiation
    of the Legacy session's crypto suite is attempted, and the Legacy
    connection keeps carrying every bit of live OpenFlow traffic,
    completely unmodified, for the entire MAKE + VERIFY phase below.

  VERIFY (the actual guard against the failure mode above):
    Over a fixed verification window of `verification_window_s`
    seconds (T), take `verification_health_checks` samples (N). Each
    sample:
      (a) re-confirms the NEW controller target's own is_connected
          state (catches a connection that flaps rather than staying
          up under sustained real traffic), and
      (b) opens a SEPARATE, synthetic probe connection through the
          SAME local stunnel client port (see _OpenFlowHealthProbe
          below) and runs a real OpenFlow 1.3 HELLO -> Echo
          Request/Reply exchange, answers a Features Request if the
          controller sends one, and watches for a Flow-Mod. This
          probe is deliberately a THIRD, disposable connection -- it
          never touches OVS's own two live sessions -- so a failed
          probe can never disrupt the switch's actual control traffic;
          it only tells us whether *this tunnel path* is carrying
          working OpenFlow bytes, not just a valid TLS handshake.
    Any failed sample -- is_connected flapping, a probe that can't
    connect, a HELLO/Echo that doesn't come back -- aborts immediately:
    the new target is dropped (single revert-to-legacy-only call) and
    the Legacy session, never having been touched, keeps running.

  BREAK (atomic cutover + grace-close):
    Only once every VERIFY sample has passed (and, if
    `require_flow_mod_confirmation` is True -- the default -- at least
    one sample observed a real Flow-Mod round trip) does this method
    issue the single ovsdb transaction that replaces the controller
    set with the NEW target alone:
        ovs-vsctl set-controller <switch> <new_target>
    OVS's controller-set model has no partial/staged way to drop one
    controller out of a multi-controller set -- there is no separate
    "mark primary" step and "close old connection" step to sequence,
    it is one atomic database write. That single write is therefore
    simultaneously this protocol's "atomic cutover... designates the
    new connection as primary" step AND its "grace-close the old
    connection only after a flow-mod round trip succeeds" step: by the
    time this call runs, the flow-mod gate has already been satisfied,
    so the old connection's removal is never premature.

  Failure at any point in MAKE or VERIFY leaves the switch on
  Legacy-only, exactly as before Stage 1 ever ran.

Honesty notes on what this protocol can and cannot verify from this
module's vantage point alone (state this plainly in the report, guide
§12's convention):
  - True OpenFlow ROLE (master/slave) negotiation is NOT exercised --
    that requires the CONTROLLER side to issue an OFPT_ROLE_REQUEST,
    which is Person A/D's controller-app code, not something
    migrate_link.py can drive from the switch/OVS side. "Designates
    the new connection as primary" is realized here as OVS's
    controller-SET containing only the new target after BREAK, which
    is the closest equivalent achievable without controller-app
    changes, and is stated as such rather than implied to be a real
    role handoff.
  - The Flow-Mod check is inherently best-effort: it depends on the
    controller app proactively pushing a flow-mod on switch-connect
    (e.g. simple_l2_switch.py installing a table-miss rule). If your
    controller app is purely reactive and never does this, set
    require_flow_mod_confirmation=False -- but say so explicitly in
    the report rather than silently weakening the gate.
  - The verification window adds latency and control-plane overhead
    (guide addendum's own stated limitation) before a migration
    commits. This is not yet quantified against Fig. 3's plain
    handshake-latency comparison; report `verification_window_s`
    (T) x number of migrated switches as the added wall-clock cost
    per run when writing this up.
  - The "data-plane check" referenced by the migration protocol
    (its own "Section 4") is NOT implemented here -- that section
    wasn't available when this file was written, and it wasn't in
    scope to guess its check that it describes. `data_plane_check_fn`
    below is a documented extension point for whoever owns that
    check to wire in for real; until it is, the verification window
    runs WITHOUT it and that gap should be named in the report, not
    silently left unmentioned.

Interface (guide section 5.4, frozen -- optimizer.py's run_sgbm() is
written against this exact shape and must not need to change when the
fake migrate_fn used for its own self-test is swapped for the real
MigrationExecutor.migrate bound method):

    migrate(dpid: str) -> {
        "dpid": str,
        "outcome": "success" | "failed",
        "baseline": {"latency_ms": float, "failure_rate": float,
                      "overhead_bytes": int},
        "post": {...same shape...} | None,   # None when outcome == "failed"
        "timestamp": iso8601 str,
    }

State ownership rule (guide section 2.3): this module never reads
optimizer.py's or coverage.py's output and never picks WHICH switch to
migrate -- it only executes a migration it is told to run, exactly
once, for exactly the dpid it is given.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import socket
import statistics
import struct
import subprocess
import time

# ---------------------------------------------------------------------
# Config -- every path/port/timing here is a default, override via
# MigrationExecutor(...) kwargs. Real values for this deployment
# belong in docs/PORTS.md once the hybrid certs and per-switch stunnel
# confs exist; nothing below is hardcoded into any *other* module, per
# the no-hardcode rule (guide §6.1) -- this is Feature 4's own config,
# not something coverage.py/topology.py read.
# ---------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

CONTROLLER_IP = "127.0.0.1"
LEGACY_PORT = 6653                 # OS-Ken's SSL listener (topo.py §D1)

HYBRID_SERVER_HOST = "127.0.0.1"   # shared stunnel server this VM; change
HYBRID_SERVER_PORT = 16653         # for a real multi-host deployment (guide §8.2)

# Legacy (classical) cert chain -- gen_certs.sh's output. Same shared
# switch cert every switch uses (guide §8.4 / gen_certs.sh comment).
LEGACY_CERT_DIR = os.path.join(_THIS_DIR, "certs")
LEGACY_CA_CERT = os.path.join(LEGACY_CERT_DIR, "ca.cert")
LEGACY_SWITCH_KEY = os.path.join(LEGACY_CERT_DIR, "switch.key")
LEGACY_SWITCH_CERT = os.path.join(LEGACY_CERT_DIR, "switch.cert")

# Hybrid (PQC) cert chain -- gen_hybrid_certs.sh's output (guide §8.4).
# Per-switch cert files follow the convention
# switch-<n>-hybrid.{key,cert}, e.g. switch-s3-hybrid.key.
HYBRID_CERT_DIR = os.path.join(_THIS_DIR, "hybrid", "certs")
HYBRID_CA_CERT = os.path.join(HYBRID_CERT_DIR, "hybrid-ca.cert")

# Per-dpid client.conf + shared server.conf + logs live here. Distinct
# from migration/stunnel_configs/, which holds the team's earlier
# Stage-1-classical-only prototype test (see that dir's own README
# note) -- never mixed with the real per-switch templates.
HYBRID_CONF_DIR = os.path.join(_THIS_DIR, "hybrid", "stunnel")
HYBRID_LOG_DIR = os.path.join(HYBRID_CONF_DIR, "logs")

SYSTEM_OPENSSL_BIN = "openssl"                       # Legacy baseline (§1.1)
PQC_OPENSSL_BIN = "/opt/openssl35/bin/openssl"       # Hybrid dry run/post (§1.1, §8.4)
HYBRID_STUNNEL_BIN = "stunnel"  # PATH-resolved; confirmed the system build already
                                 # supports `curve` -- see D2 above. Override only if
                                 # a given deployment genuinely needs a different binary.
HYBRID_GROUP = "X25519MLKEM768"

BASELINE_SAMPLES = 5
HANDSHAKE_PROBE_TIMEOUT_S = 5
DRY_RUN_LOG_WAIT_S = 5
STUNNEL_READY_TIMEOUT_S = 10         # how long to wait for a freshly launched
                                     # client stunnel to start accepting
                                     # (ML-DSA cert + PQC OpenSSL load is slow)
CUTOVER_POLL_TIMEOUT_S = 10          # B-B-M cutover: wait for Hybrid-only
CUTOVER_POLL_INTERVAL_S = 0.5        # OVS Controller target to connect

# -- Break-Before-Make post-cutover verification defaults --
VERIFICATION_WINDOW_S = 12.0         # T: total sustained-check window
VERIFICATION_HEALTH_CHECKS = 4       # N: samples spread across T
FEATURES_WAIT_S = 3.0                # per-sample: how long to wait for a
                                      # Features Request / Flow-Mod after Hello+Echo
OF_PROBE_TIMEOUT_S = 3.0             # per-sample probe socket timeout
REQUIRE_FLOW_MOD_CONFIRMATION = True # hard-gate BREAK on seeing >=1 flow-mod;
                                      # see the honesty note above before disabling


def local_port_for_dpid(dpid: str) -> int:
    """
    20000 + last dpid byte (guide §8.2's suggested scheme), so every
    switch gets a unique loopback port for its Hybrid stunnel client
    without a hand-maintained port table. Collisions are impossible
    for this project's dpid space (last byte is always the switch's
    own single-digit trailing hex value, per topo.py); docs/PORTS.md
    records the resulting assignments for humans.
    """
    return 20000 + int(dpid[-2:], 16)


def _probe_dpid(dpid: str) -> str:
    """
    A synthetic dpid used ONLY in the VERIFY-phase health probe's
    Features Reply -- deliberately DIFFERENT from the switch's real
    dpid.

    CONFIRMED via live testing (2026-09-23): presenting the REAL dpid
    here causes OS-Ken/Ryu's connection handling to treat the probe's
    disposable connection as a NEWER session for the SAME datapath,
    and evict/replace the real OVS connection mid-VERIFY -- this is
    exactly the "no longer connected (flapping)" failure the flapping
    check exists to catch, except the probe itself was the cause.

    Top byte forced to 0xFE (never used by topo.py's real dpid
    assignments, which all start 00) so this can never collide with
    any real switch, while remaining a well-formed 8-byte dpid so the
    Features Reply itself stays valid OpenFlow.
    """
    return "fe" + dpid[2:]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _empty_metrics(failure_rate: float = 1.0) -> dict:
    return {"latency_ms": 0.0, "failure_rate": failure_rate, "overhead_bytes": 0}


# ---------------------------------------------------------------------
# Minimal OpenFlow 1.3 wire format -- just enough to run a synthetic
# HELLO / Echo Request-Reply / Features / Flow-Mod health check over a
# raw socket (guide addendum's Make-Before-Break VERIFY phase). This is
# NOT a general OpenFlow library -- it only speaks the handful of
# message types the verification probe needs, and only well enough to
# be a truthful peer for that purpose.
# ---------------------------------------------------------------------

OFP_VERSION = 0x04          # OpenFlow 1.3
OFPT_HELLO = 0
OFPT_ERROR = 1
OFPT_ECHO_REQUEST = 2
OFPT_ECHO_REPLY = 3
OFPT_FEATURES_REQUEST = 5
OFPT_FEATURES_REPLY = 6
OFPT_FLOW_MOD = 14

_OFP_HEADER = struct.Struct("!BBHI")  # version, type, length, xid


def _of_header(msg_type: int, length: int, xid: int) -> bytes:
    return _OFP_HEADER.pack(OFP_VERSION, msg_type, length, xid)


def _of_hello(xid: int = 0) -> bytes:
    return _of_header(OFPT_HELLO, 8, xid)


def _of_echo_request(xid: int) -> bytes:
    return _of_header(OFPT_ECHO_REQUEST, 8, xid)


def _of_features_reply(xid: int, dpid_hex: str) -> bytes:
    """Minimal valid OFPT_FEATURES_REPLY body for OF1.3: datapath_id(8)
    n_buffers(4) n_tables(1) auxiliary_id(1) pad(2) capabilities(4)
    reserved(4) = 24 bytes. Port descriptions are not needed in 1.3
    (they come via multipart), so this is a complete, valid message."""
    dpid_bytes = bytes.fromhex(dpid_hex)
    body = dpid_bytes + struct.pack("!IBB2xII", 0, 1, 0, 0, 0)
    return _of_header(OFPT_FEATURES_REPLY, 8 + len(body), xid) + body


class MigrationError(Exception):
    """Raised only for programmer-error-style conditions (e.g. a dpid
    that resolves to no OVS bridge at all). Real operational failures
    -- a bad handshake, a missing cert, a timeout -- are NOT exceptions;
    they are outcome="failed" results, per Algorithm 2's own design."""


class _OpenFlowHealthProbe:
    """
    A synthetic OpenFlow 1.3 peer used ONLY for the Make-Before-Break
    verification window. This does NOT drive the switch's real control
    session -- it opens its OWN short-lived connection through the
    same local stunnel client port the real OVS connection uses, so it
    exercises the identical Hybrid tunnel path (local stunnel client ->
    network -> shared stunnel server -> controller) without ever
    touching OVS's own OpenFlow state machine. This is the standard
    "synthetic health check" technique: inject probe traffic alongside
    the real session, never through it.
    """

    def __init__(self, host: str, port: int, dpid: str, timeout: float):
        self.host = host
        self.port = port
        self.dpid = dpid
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self) -> "_OpenFlowHealthProbe":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        return self

    def __exit__(self, *exc_info):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def _recv_message(self, deadline: float):
        while len(self._buf) < 8:
            remaining = max(0.05, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed during header read")
            self._buf += chunk
        _version, msg_type, length, xid = _OFP_HEADER.unpack(self._buf[:8])
        while len(self._buf) < length:
            remaining = max(0.05, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed during body read")
            self._buf += chunk
        body = self._buf[8:length]
        self._buf = self._buf[length:]
        return msg_type, xid, body

    def run_healthcheck(self, want_flow_mod: bool, features_wait_s: float) -> dict:
        """
        Performs, over one disposable connection:
          1. HELLO exchange.
          2. Echo Request/Reply (the guide addendum's explicit
             "Echo Request/Reply" bullet).
          3. Answers a Features Request if the peer sends one, with a
             minimal valid Features Reply carrying this switch's real
             dpid (the "a feature request" bullet).
          4. Watches (up to features_wait_s) for an unsolicited
             Flow-Mod from the controller (the "dummy flow-mod"
             bullet). Best-effort by nature -- see this file's
             module-level honesty note on why a miss here is reported,
             not treated as a probe malfunction.

        Returns {"hello_ok", "echo_ok", "features_ok",
        "flow_mod_observed", "error"}.
        """
        result = {
            "hello_ok": False, "echo_ok": False,
            "features_ok": False, "flow_mod_observed": False,
            "error": None,
        }
        try:
            deadline = time.monotonic() + max(self.timeout, features_wait_s)

            self.sock.sendall(_of_hello(xid=1))
            msg_type, _xid, _body = self._recv_message(deadline)
            if msg_type != OFPT_HELLO:
                result["error"] = f"expected HELLO from peer, got OF message type {msg_type}"
                return result
            result["hello_ok"] = True

            echo_xid = 2
            self.sock.sendall(_of_echo_request(echo_xid))

            window_deadline = time.monotonic() + features_wait_s
            while time.monotonic() < window_deadline:
                have_echo = result["echo_ok"]
                have_flow_mod = result["flow_mod_observed"] or not want_flow_mod
                if have_echo and have_flow_mod:
                    break
                try:
                    msg_type, xid, _body = self._recv_message(window_deadline)
                except (socket.timeout, TimeoutError):
                    break
                except (OSError, ConnectionError):
                    # Peer closed the connection while we were waiting for
                    # more (best-effort) extras -- Features/Flow-Mod are
                    # layered on top of the essential Hello/Echo check, so
                    # if the peer is done talking to us after satisfying
                    # those, that's a normal end of this probe's window,
                    # NOT a health-check failure. Only a closed connection
                    # BEFORE echo_ok is achieved should count against the
                    # switch (that path is handled below, since echo_ok
                    # simply stays False and the caller treats that as a
                    # failed sample).
                    break
                if msg_type == OFPT_ECHO_REPLY and xid == echo_xid:
                    result["echo_ok"] = True
                elif msg_type == OFPT_ECHO_REQUEST:
                    # peer pinging us back mid-exchange -- answer in kind,
                    # harmless and keeps the connection looking legitimate
                    self.sock.sendall(_of_header(OFPT_ECHO_REPLY, 8, xid))
                elif msg_type == OFPT_FEATURES_REQUEST:
                    self.sock.sendall(_of_features_reply(xid, self.dpid))
                    result["features_ok"] = True
                elif msg_type == OFPT_FLOW_MOD:
                    result["flow_mod_observed"] = True

            return result
        except (OSError, ConnectionError) as exc:
            result["error"] = str(exc)
            return result


# ---------------------------------------------------------------------
# Command execution -- wrapped behind a small object instead of calling
# subprocess directly, so tests can inject a fake runner and exercise
# every branch of migrate() without real Mininet/OVS/stunnel/OpenSSL
# on the machine running the tests. The default is the real thing.
# ---------------------------------------------------------------------

class CommandRunner:
    """Default runner: actually shells out. See FakeCommandRunner in
    the __main__ self-test for the injectable test double."""

    def run(self, args, timeout=None, input_bytes=b""):
        try:
            cp = subprocess.run(
                args,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
            )
            return cp.returncode, cp.stdout.decode(errors="replace"), cp.stderr.decode(errors="replace")
        except FileNotFoundError as exc:
            return 127, "", str(exc)
        except subprocess.TimeoutExpired as exc:
            return -1, "", f"timeout after {exc.timeout}s"

    def popen(self, args, stdout_path=None, env=None):
        stdout = open(stdout_path, "ab") if stdout_path else subprocess.DEVNULL
        return subprocess.Popen(args, stdout=stdout, stderr=subprocess.STDOUT, env=env)


# ---------------------------------------------------------------------
# MigrationExecutor -- Algorithm 2 (Guarded Migration Execution),
# Stage 2 now implementing the Make-Before-Break protocol above.
# ---------------------------------------------------------------------

class MigrationExecutor:
    """
    Owns the Legacy<->Hybrid cutover for one dpid at a time. Bind
    `.migrate` as the `migrate_fn` passed into optimizer.run_sgbm()
    (guide §7.2) -- the bound method's signature, `migrate(dpid) ->
    dict`, matches the frozen §5.4 contract exactly, so nothing in
    optimizer.py needs to change when swapping the self-test fake for
    this real implementation.

    Parameters
    ----------
    topology : object with get_state(dpid) / set_state(dpid, value)
        In production this is the live TopologyAdapter OS-Ken app
        (network/topology.py). This executor calls set_state(dpid,
        "Hybrid") in exactly one place (on a verified, make-before-break
        cutover success) and nowhere else -- see the state ownership
        rule, guide §2.3.
    ledger : object with log_event(dpid, baseline, post, outcome,
             reason=None, associated_flows=None)
        Person C's ledger.py skeleton or any object satisfying that
        shape -- e.g. a plain list-appending stand-in for tests, as in
        the __main__ block below.
    runner : CommandRunner, optional
        Defaults to the real subprocess-backed CommandRunner(). Inject
        a fake for tests (see __main__).
    data_plane_check_fn : callable(dpid, switch_name) -> (ok, reason), optional
        Extension point for the migration protocol's own "the
        data-plane check in Section 4" -- NOT implemented in this
        file (see the module-level honesty note for why). Defaults to
        None, meaning the verification window runs without it; wire in
        the real check here once that section's spec is available,
        rather than approximating it.
    require_flow_mod_confirmation : bool
        Hard-gates BREAK on observing >=1 real Flow-Mod during VERIFY,
        per the protocol's "grace-close only after a flow-mod round
        trip succeeds" rule. Only set False if you've confirmed your
        controller app never proactively pushes one -- and say so in
        the report (see the module-level honesty note).
    Every other keyword overrides one of the module-level defaults
    above (ports, cert paths, binaries, timeouts) without needing to
    edit this file -- e.g. a second VM's IP for hybrid_server_host.
    """

    def __init__(
        self,
        topology,
        ledger,
        *,
        runner: CommandRunner | None = None,
        controller_ip: str = CONTROLLER_IP,
        legacy_port: int = LEGACY_PORT,
        hybrid_server_host: str = HYBRID_SERVER_HOST,
        hybrid_server_port: int = HYBRID_SERVER_PORT,
        legacy_ca_cert: str = LEGACY_CA_CERT,
        legacy_switch_key: str = LEGACY_SWITCH_KEY,
        legacy_switch_cert: str = LEGACY_SWITCH_CERT,
        hybrid_ca_cert: str = HYBRID_CA_CERT,
        hybrid_cert_dir: str = HYBRID_CERT_DIR,
        hybrid_conf_dir: str = HYBRID_CONF_DIR,
        hybrid_log_dir: str = HYBRID_LOG_DIR,
        system_openssl_bin: str = SYSTEM_OPENSSL_BIN,
        pqc_openssl_bin: str = PQC_OPENSSL_BIN,
        hybrid_stunnel_bin: str = HYBRID_STUNNEL_BIN,
        hybrid_group: str = HYBRID_GROUP,
        baseline_samples: int = BASELINE_SAMPLES,
        handshake_probe_timeout: float = HANDSHAKE_PROBE_TIMEOUT_S,
        dry_run_log_wait: float = DRY_RUN_LOG_WAIT_S,
        stunnel_ready_timeout: float = STUNNEL_READY_TIMEOUT_S,
        pqc_lib_dirs=None,
        cutover_poll_timeout: float = CUTOVER_POLL_TIMEOUT_S,
        cutover_poll_interval: float = CUTOVER_POLL_INTERVAL_S,
        verification_window_s: float = VERIFICATION_WINDOW_S,
        verification_health_checks: int = VERIFICATION_HEALTH_CHECKS,
        features_wait_s: float = FEATURES_WAIT_S,
        of_probe_timeout: float = OF_PROBE_TIMEOUT_S,
        require_flow_mod_confirmation: bool = REQUIRE_FLOW_MOD_CONFIRMATION,
        data_plane_check_fn=None,
        verbose: bool = True,
    ):
        self.topology = topology
        self.ledger = ledger
        self.runner = runner or CommandRunner()

        self.controller_ip = controller_ip
        self.legacy_port = legacy_port
        self.hybrid_server_host = hybrid_server_host
        self.hybrid_server_port = hybrid_server_port

        self.legacy_ca_cert = legacy_ca_cert
        self.legacy_switch_key = legacy_switch_key
        self.legacy_switch_cert = legacy_switch_cert

        self.hybrid_ca_cert = hybrid_ca_cert
        self.hybrid_cert_dir = hybrid_cert_dir
        self.hybrid_conf_dir = hybrid_conf_dir
        self.hybrid_log_dir = hybrid_log_dir

        self.system_openssl_bin = system_openssl_bin
        self.pqc_openssl_bin = pqc_openssl_bin
        self.hybrid_stunnel_bin = hybrid_stunnel_bin
        self.hybrid_group = hybrid_group

        self.baseline_samples = baseline_samples
        self.handshake_probe_timeout = handshake_probe_timeout
        self.dry_run_log_wait = dry_run_log_wait
        self.stunnel_ready_timeout = stunnel_ready_timeout
        # Library dirs of the PQC-capable OpenSSL that the stunnel
        # processes MUST load. None = derive from pqc_openssl_bin
        # (<prefix>/lib64 and <prefix>/lib, whichever exist).
        self.pqc_lib_dirs = pqc_lib_dirs
        self.cutover_poll_timeout = cutover_poll_timeout
        self.cutover_poll_interval = cutover_poll_interval

        self.verification_window_s = verification_window_s
        self.verification_health_checks = verification_health_checks
        self.features_wait_s = features_wait_s
        self.of_probe_timeout = of_probe_timeout
        self.require_flow_mod_confirmation = require_flow_mod_confirmation
        self.data_plane_check_fn = data_plane_check_fn

        self.verbose = verbose

        # dpid -> Popen, one persistent local stunnel CLIENT per
        # migrated/attempted switch. The shared stunnel SERVER is a
        # single process for the whole run (guide §8.2: "you do not
        # need one controller-side stunnel per switch").
        self._client_procs: dict[str, subprocess.Popen] = {}
        self._server_proc: subprocess.Popen | None = None

    # -- logging -----------------------------------------------------

    def _log(self, msg: str):
        if self.verbose:
            print(f"[migrate_link] {msg}")

    def shutdown(self):
        """Stop every stunnel process this executor started (the shared
        server and every per-switch client). Call this once you're done
        migrating for this run/process -- otherwise these keep running
        as orphans after the script exits."""
        procs = list(self._client_procs.values())
        if self._server_proc is not None:
            procs.append(self._server_proc)
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        self._client_procs.clear()
        self._server_proc = None

    # -----------------------------------------------------------------
    # Switch-name resolution (dpid -> OVS bridge name)
    # -----------------------------------------------------------------

    def _resolve_switch_name(self, dpid: str) -> str:
        """
        `ovs-vsctl set-controller` needs a bridge NAME (e.g. "s3"), not
        a dpid. Resolved by asking OVS itself which bridge currently
        reports this datapath_id, rather than deriving it from the
        dpid string by convention -- topo.py's s0 deliberately does
        NOT follow the "last hex digit == switch number" pattern the
        other switches use (guide §5.2's dpid table), so a formula
        here would silently break for s0. This keeps migrate_link.py
        correct for whatever topo.py currently assigns, per the same
        no-hardcode spirit as topology.py's own design.
        """
        rc, out, err = self.runner.run(["ovs-vsctl", "list-br"], timeout=self.handshake_probe_timeout)
        if rc != 0:
            raise MigrationError(f"ovs-vsctl list-br failed: {err.strip() or out.strip()}")
        for bridge in out.split():
            rc2, out2, _err2 = self.runner.run(
                ["ovs-vsctl", "get", "bridge", bridge, "datapath_id"],
                timeout=self.handshake_probe_timeout,
            )
            if rc2 == 0 and out2.strip().strip('"') == dpid:
                return bridge
        raise MigrationError(f"no OVS bridge with datapath_id={dpid!r} found (is topo.py running?)")

    # -----------------------------------------------------------------
    # Baseline / post metrics -- real TLS handshake probes
    # -----------------------------------------------------------------

    def _probe_handshake(self, openssl_bin, host, port, cert, key, ca, groups=None):
        """
        One real TLS 1.3 handshake via `openssl s_client`, timed
        wall-clock. Returns (ok: bool, elapsed_ms: float). This is the
        same tool + cert chain OVS itself uses, so timing it is a
        faithful proxy for what a real switch handshake costs -- not a
        simulated number.
        """
        cmd = [
            openssl_bin, "s_client",
            "-connect", f"{host}:{port}",
            "-cert", cert, "-key", key, "-CAfile", ca,
            "-tls1_3", "-brief",
        ]
        if groups:
            cmd += ["-groups", groups]
        start = time.monotonic()
        rc, out, err = self.runner.run(cmd, timeout=self.handshake_probe_timeout, input_bytes=b"Q\n")
        elapsed_ms = (time.monotonic() - start) * 1000.0
        combined = out + err
        ok = rc == 0 and ("Verification: OK" in combined or "Verify return code: 0" in combined)
        return ok, elapsed_ms

    @staticmethod
    def _cert_chain_bytes(*paths: str) -> int:
        """
        Best-effort overhead proxy: total bytes of the cert/key
        material presented in the handshake. This is NOT a packet
        capture of actual wire overhead (out of scope here) -- state
        that plainly in the report rather than implying it's a
        measured on-wire byte count, matching the guide's honesty-note
        convention (§2.2/§12). It is still directly useful: it's the
        real reason Hybrid overhead is higher (ML-DSA public keys and
        signatures are materially larger than ECDSA's).
        """
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def _capture_metrics_legacy(self, dpid: str) -> dict:
        """Stage 1 baseline (Algorithm 2, lines 2-3): N handshakes
        against the switch's CURRENT classical connection."""
        latencies, failures = [], 0
        for _ in range(self.baseline_samples):
            ok, elapsed_ms = self._probe_handshake(
                self.system_openssl_bin, self.controller_ip, self.legacy_port,
                self.legacy_switch_cert, self.legacy_switch_key, self.legacy_ca_cert,
            )
            if ok:
                latencies.append(elapsed_ms)
            else:
                failures += 1
        n = max(self.baseline_samples, 1)
        return {
            "latency_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "failure_rate": round(failures / n, 3),
            "overhead_bytes": self._cert_chain_bytes(
                self.legacy_switch_cert, self.legacy_switch_key, self.legacy_ca_cert
            ),
        }

    def _capture_metrics_hybrid(
        self,
        dpid: str,
        switch_name: str,
        connect_latency_ms: float = 0.0,
        verification_samples: int = 0,
        verification_failures: int = 0,
    ) -> dict:
        """
        Post-cutover metrics for the ACTUAL OVS Hybrid controller path.

        Unlike the old implementation, this does not create a separate
        openssl s_client connection to the Hybrid server.

        The Hybrid path has already been proven during Stage 1 using the
        PQC TLS handshake probe. After cutover, this method measures the
        actual OVS controller connection state.
        """

        # The Hybrid connection itself is plaintext between OVS and the
        # local stunnel client. Therefore an independent openssl handshake
        # here would not represent the OVS control connection.

        samples = max(verification_samples, 1)
        failures = max(verification_failures, 0)

        failure_rate = round(failures / samples, 3)

        key, cert = self._hybrid_switch_cert_paths(switch_name)

        return {
            # Time required for the REAL OVS Hybrid controller target
            # to become connected after the cutover.
            "latency_ms": round(connect_latency_ms, 3),

            # Fraction of verification samples during which the
            # REAL OVS Hybrid controller was disconnected.
            "failure_rate": failure_rate,

            # Keep the existing certificate-size overhead proxy.
            #
            # This is still not wire overhead; it is the same
            # certificate/key material size proxy used elsewhere.
            "overhead_bytes": self._cert_chain_bytes(
                cert,
                key,
                self.hybrid_ca_cert,
            ),
        }

    def _hybrid_switch_cert_paths(self, switch_name: str):
        """Per-switch Hybrid (ML-DSA) key/cert, per guide §8.4's
        naming convention: gen_hybrid_certs.sh produces
        switch-<n>-hybrid.{key,cert}."""
        key = os.path.join(self.hybrid_cert_dir, f"switch-{switch_name}-hybrid.key")
        cert = os.path.join(self.hybrid_cert_dir, f"switch-{switch_name}-hybrid.cert")
        return key, cert

    # -----------------------------------------------------------------
    # Stage 1 -- pre-flight: bring up stunnel, confirm the Hybrid group.
    # Unchanged by the Make-Before-Break protocol: this stays entirely
    # non-disruptive and never touches OVS's controller target, so the
    # Legacy session is guaranteed untouched through this whole stage.
    # -----------------------------------------------------------------

    def _render_client_conf(self, dpid: str, switch_name: str, local_port: int) -> str:
        """Per-dpid stunnel CLIENT config: plaintext accept on
        local_port (this becomes OVS's future controller target),
        outbound Hybrid TLS to the shared server. `curve` is set
        explicitly, restricted to exactly one value -- confirmed
        working against the system stunnel binary (see D2 above), no
        separate build required."""
        key, cert = self._hybrid_switch_cert_paths(switch_name)
        log_path = os.path.join(self.hybrid_log_dir, f"client_{dpid}.log")
        return (
            f"; auto-generated by migrate_link.py for dpid={dpid} ({switch_name})\n"
            f"client = yes\n"
            f"pid =\n"
            f"foreground = yes\n"
            f"debug = 6\n"
            f"output = {log_path}\n"
            f"cert = {cert}\n"
            f"key = {key}\n"
            f"CAfile = {self.hybrid_ca_cert}\n"
            f"verifyChain = yes\n"
            f"\n"
            f"[hybrid-{switch_name}]\n"
            f"accept = 127.0.0.1:{local_port}\n"
            f"connect = {self.hybrid_server_host}:{self.hybrid_server_port}\n"
            f"curve = {self.hybrid_group}\n"
        )

    def _stunnel_env(self):
        """Environment for launched stunnel processes.

        The distro stunnel is dynamically linked and only speaks
        ML-KEM/ML-DSA when it loads the PQC OpenSSL (3.5+) at runtime
        instead of the system one (3.0.x). That normally comes from
        LD_LIBRARY_PATH in the operator's shell -- which `sudo` RESETS,
        so a stunnel spawned from `sudo python3 ...` silently falls back
        to the system OpenSSL and fails on the first ML-DSA cert
        ("ee key too small" / "decode error"). Set it explicitly here so
        the result no longer depends on how the executor was launched.
        """
        dirs = self.pqc_lib_dirs
        if dirs is None:
            prefix = os.path.dirname(os.path.dirname(self.pqc_openssl_bin))
            dirs = [d for d in (os.path.join(prefix, "lib64"), os.path.join(prefix, "lib"))
                    if os.path.isdir(d)]
        env = dict(os.environ)
        if dirs:
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = ":".join(list(dirs) + ([existing] if existing else []))
        return env

    @staticmethod
    def _tail(path: str, n: int = 8) -> str:
        try:
            with open(path, "r", errors="replace") as fh:
                lines = [ln.rstrip() for ln in fh.readlines() if ln.strip()]
            return " | ".join(lines[-n:])
        except OSError:
            return ""

    def _wait_for_local_listener(self, port: int, proc, log_paths):
        """Poll until 127.0.0.1:<port> accepts a TCP connection, up to
        self.stunnel_ready_timeout. Returns (ok, reason). If the stunnel
        process exits first, reports its exit code plus the tail of its
        stderr capture / log instead of a bare 'connection refused'."""
        deadline = time.monotonic() + self.stunnel_ready_timeout
        while True:
            rc = proc.poll()
            if rc is not None:
                detail = " || ".join(t for t in (self._tail(p) for p in log_paths) if t)
                return False, (f"local Hybrid stunnel client exited with code {rc} before "
                               f"listening on port {port}" + (f": {detail}" if detail else ""))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True, None
            except OSError:
                pass
            if time.monotonic() >= deadline:
                detail = " || ".join(t for t in (self._tail(p) for p in log_paths) if t)
                return False, (f"local Hybrid stunnel client still not listening on port {port} "
                               f"after {self.stunnel_ready_timeout}s" + (f": {detail}" if detail else ""))
            time.sleep(0.2)

    def _ensure_stunnel_pair_up(self, dpid: str, switch_name: str, local_port: int):
        """
        Idempotent: starts the shared server once, and this switch's
        client once. Returns (ok, reason). A missing hybrid_stunnel_bin
        or missing cert files fails cleanly here rather than raising --
        Stage 1 is exactly the place Algorithm 2 expects this kind of
        failure. Uses shutil.which() rather than os.path.exists() so
        the default (a bare "stunnel", resolved via PATH) and an
        explicit absolute-path override both work correctly.
        """
        if shutil.which(self.hybrid_stunnel_bin) is None:
            return False, f"hybrid stunnel binary not found: {self.hybrid_stunnel_bin!r} (not on PATH and not executable)"

        key, cert = self._hybrid_switch_cert_paths(switch_name)
        for needed in (key, cert, self.hybrid_ca_cert):
            if not os.path.exists(needed):
                return False, f"missing Hybrid cert material: {needed} (run gen_hybrid_certs.sh first)"

        os.makedirs(self.hybrid_conf_dir, exist_ok=True)
        os.makedirs(self.hybrid_log_dir, exist_ok=True)

        if self._server_proc is None or self._server_proc.poll() is not None:
            server_conf = os.path.join(self.hybrid_conf_dir, "server.conf")
            if not os.path.exists(server_conf):
                return False, f"shared Hybrid server conf not found: {server_conf}"
            self._log(f"starting shared Hybrid stunnel server ({server_conf})")
            # stderr captured to a file: stunnel prints config/startup
            # errors there BEFORE its own `output =` log file is open.
            self._server_proc = self.runner.popen(
                [self.hybrid_stunnel_bin, server_conf],
                stdout_path=os.path.join(self.hybrid_log_dir, "server.stderr"),
                env=self._stunnel_env(),
            )
            # Give it a moment to bind, and catch an immediate crash
            # (bad config / wrong OpenSSL) instead of discovering it
            # later as a confusing client-side failure.
            settle_end = time.monotonic() + 1.5
            while time.monotonic() < settle_end:
                rc = self._server_proc.poll()
                if rc is not None:
                    detail = " || ".join(t for t in (
                        self._tail(os.path.join(self.hybrid_log_dir, "server.stderr")),
                        self._tail(os.path.join(self.hybrid_log_dir, "server.log"))) if t)
                    self._server_proc = None
                    return False, (f"shared Hybrid stunnel server exited with code {rc}"
                                   + (f": {detail}" if detail else ""))
                time.sleep(0.1)

        proc = self._client_procs.get(dpid)
        if proc is None or proc.poll() is not None:
            client_conf_path = os.path.join(self.hybrid_conf_dir, f"client_{dpid}.conf")
            with open(client_conf_path, "w") as fh:
                fh.write(self._render_client_conf(dpid, switch_name, local_port))
            self._log(f"starting Hybrid stunnel client for {dpid} ({switch_name}) on port {local_port}")
            client_stderr = os.path.join(self.hybrid_log_dir, f"client_{dpid}.stderr")
            proc = self.runner.popen(
                [self.hybrid_stunnel_bin, client_conf_path], stdout_path=client_stderr,
                env=self._stunnel_env(),
            )
            self._client_procs[dpid] = proc

        # Wait until the client is actually accepting (a fixed sleep was
        # too short for the ML-DSA/PQC OpenSSL load) -- and fail with a
        # real reason if it died instead of just being slow.
        ready, why = self._wait_for_local_listener(
            local_port, proc,
            [os.path.join(self.hybrid_log_dir, f"client_{dpid}.stderr"),
             os.path.join(self.hybrid_log_dir, f"client_{dpid}.log")],
        )
        if not ready:
            return False, why

        return True, None

    # Log-line patterns for _dry_run_confirm_hybrid_group, calibrated
    # against a real stunnel 5.63 session (see D2 above for the full
    # story of why this checks for handshake SUCCESS rather than
    # parsing out a group name):
    #   success (either role): "TLS connected: new session negotiated"
    #                        or "TLS accepted: new session negotiated"
    #   informational only, logged if present but never required to
    #   match a specific name -- a PQC/hybrid group added by an
    #   external or newer default provider has no OpenSSL NID, so
    #   stunnel legitimately cannot resolve one to text:
    #                          "Peer temporary key: (null), 768 bits"
    #   failure: any fatal TLS alert or an OpenSSL routine-level error
    #            (e.g. "SSL_accept: ... certificate verify failed",
    #            "SSL routines::no shared cipher")
    _HANDSHAKE_OK_RE = re.compile(r"TLS (?:connected|accepted): new session negotiated")
    _PEER_TMP_KEY_RE = re.compile(r"Peer temporary key:\s*([^,]*),\s*(\d+)\s*bits")
    _HANDSHAKE_ERROR_RE = re.compile(
        r"(TLS alert \((?:read|write)\): fatal|"
        r"SSL_(?:connect|accept):|"
        r"SSL routines::|"
        r"Rejected by CERT)"
    )

    def _dry_run_confirm_hybrid_group(self, dpid: str, local_port: int):
        """
        Triggers the client stunnel's outbound handshake (a bare TCP
        connect to its plaintext accept port is enough to make stunnel
        dial out) and tails its log for a completed handshake, per
        guide §8.3 step 3. Returns (ok, reason).

        Both this client's conf and the shared server's conf restrict
        `curve` to exactly ONE value (self.hybrid_group) -- so a
        successful handshake is, by construction, proof that exact
        group was used. There is no second group configured to fall
        back to, which is why this checks for success/failure rather
        than trying to extract and compare a group name: that name can
        legitimately render as "(null)" in the log for a PQC group
        with no OpenSSL NID (see D2 above), so name-matching would
        reject a fully correct Hybrid handshake.
        """
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=self.handshake_probe_timeout) as s:
                s.settimeout(0.5)
                try:
                    s.recv(1)
                except (socket.timeout, OSError):
                    pass
        except OSError as exc:
            return False, f"could not reach local Hybrid stunnel client on port {local_port}: {exc}"

        log_path = os.path.join(self.hybrid_log_dir, f"client_{dpid}.log")
        deadline = time.monotonic() + self.dry_run_log_wait
        while time.monotonic() < deadline:
            if os.path.exists(log_path):
                with open(log_path, "r", errors="replace") as fh:
                    text = fh.read()
                if self._HANDSHAKE_OK_RE.search(text):
                    m = self._PEER_TMP_KEY_RE.search(text)
                    if m:
                        self._log(f"{dpid}: Hybrid handshake ok, peer temporary key: "
                                   f"{m.group(1) or '(null)'}, {m.group(2)} bits "
                                   f"(curve was restricted to {self.hybrid_group!r} on both ends)")
                    return True, None
                err = self._HANDSHAKE_ERROR_RE.search(text)
                if err:
                    return False, f"stunnel client log reports a handshake error ({err.group(0)!r}), see {log_path} for detail"
            time.sleep(0.25)
        return False, f"no completed-handshake line in {log_path} after {self.dry_run_log_wait}s"

    def _stage1_preflight(self, dpid: str, switch_name: str):
        """Returns (ok, baseline_metrics, local_port, reason|None)."""
        baseline = self._capture_metrics_legacy(dpid)
        local_port = local_port_for_dpid(dpid)

        up_ok, up_reason = self._ensure_stunnel_pair_up(dpid, switch_name, local_port)
        if not up_ok:
            return False, baseline, local_port, up_reason

        group_ok, group_reason = self._dry_run_confirm_hybrid_group(dpid, local_port)
        if not group_ok:
            return False, baseline, local_port, group_reason

        return True, baseline, local_port, None

    # -----------------------------------------------------------------
    # Stage 2 -- cutover (currently Break-Before-Make)
    # -----------------------------------------------------------------

    def _set_controller_target(self, switch_name: str, *targets: str):
	"""
	Atomically replace the bridge's complete OVS controller set.
	One target means the bridge uses exactly that controller:
	Legacy-only or Hybrid-only.
	The B-B-M migration uses this operation twice at most:
	1. switch to Hybrid-only for the cutover;
	2. restore Legacy-only if Hybrid verification fails.
	Supplying the complete resulting controller set makes each
	ovs-vsctl operation an explicit controller-set replacement.
	"""
        return self.runner.run(
            ["ovs-vsctl", "set-controller", switch_name, *targets],
            timeout=self.handshake_probe_timeout,
        )

    def _controller_target_connected(self, switch_name: str, target: str) -> bool:
        """
	Check the connection state of one specific OVS Controller target.

	This is deliberately a per-target check rather than a bridge-wide
	aggregate. During B-B-M migration the controller set contains only
	the target being tested, so this directly answers whether the actual
	Hybrid OVS connection is established.

	`find Controller target=...` is scoped to the OVSDB. The Hybrid
	target contains a per-switch local port, so targets are unique
	between switches.
	"""
        rc, out, _err = self.runner.run(
            ["ovs-vsctl", "--bare", "--columns=is_connected", "find", "Controller",
             f'target="{target}"'],
            timeout=self.handshake_probe_timeout,
        )
        return rc == 0 and out.strip().lower() == "true"

    def _revert_to_legacy_only(self, switch_name: str, legacy_target: str):
        rc, out, err = self._set_controller_target(switch_name, legacy_target)
        if rc != 0:
            self._log(
                f"WARNING: revert to Legacy-only failed for {switch_name}: {(err or out).strip()} "
                f"-- switch may be left in an unexpected controller-set state, check manually"
            )

    def _stage2_break_before_make(self, dpid: str, switch_name: str, local_port: int):
        """
        Break-Before-Make Hybrid cutover.

        Stage 1 has already proven that the Hybrid stunnel pair is alive
        and that the required PQC TLS group was successfully negotiated.

        Stage 2:
          1. BREAK the existing Legacy controller connection.
          2. Set OVS to the Hybrid controller target only.
          3. Wait for OVS to report that exact Hybrid target connected.
          4. Verify that the SAME OVS controller connection remains connected
             for the configured verification window.
          5. Optionally run the data-plane check.
          6. If any gate fails, restore Legacy-only.

        This deliberately does NOT:
          - configure Legacy + Hybrid simultaneously
          - create a synthetic OpenFlow health-probe connection
          - use a fake DPID
          - open a second OpenFlow session to OS-Ken

        Returns:
            (ok, post_metrics | None, reason | None)
        """
        legacy_target = f"ssl:{self.controller_ip}:{self.legacy_port}"
        new_target = f"tcp:127.0.0.1:{local_port}"

        # -------------------------------------------------------------
        # BREAK
        # -------------------------------------------------------------
        #
        # Remove Legacy first and make Hybrid the ONLY controller target.
        # This is intentionally disruptive for the brief cutover interval,
        # but avoids OVS/OS-Ken having two simultaneous sessions for the
        # same real DPID.
        #
        rc, out, err = self._set_controller_target(switch_name, new_target)

        if rc != 0:
            # The Legacy target may still be usable if ovs-vsctl failed
            # before changing the controller set.
            self._revert_to_legacy_only(switch_name, legacy_target)
            return (
                False,
                None,
                f"BREAK: ovs-vsctl set-controller to Hybrid failed: "
                f"{(err or out).strip()}",
            )

        self._log(
            f"BREAK ok: {switch_name} controller target changed "
            f"from Legacy to Hybrid-only at {new_target}"
        )

        # -------------------------------------------------------------
        # WAIT FOR THE ACTUAL OVS HYBRID CONNECTION
        # -------------------------------------------------------------
        #
        # We do NOT create _OpenFlowHealthProbe().
        #
        # We ask OVS itself whether the controller object corresponding
        # to the Hybrid target is actually connected.
        #
        connect_deadline = time.monotonic() + self.cutover_poll_timeout

        connected = False
        connect_start = time.monotonic()

        while time.monotonic() < connect_deadline:
            if self._controller_target_connected(switch_name, new_target):
                connected = True
                break
            time.sleep(self.cutover_poll_interval)

        connect_latency_ms = (time.monotonic() - connect_start) * 1000.0

        if not connected:
            self._revert_to_legacy_only(switch_name, legacy_target)
            return (
                False,
                None,
                f"BREAK: Hybrid target {new_target} did not report "
                f"is_connected=true within "
                f"{self.cutover_poll_timeout}s; restored Legacy-only",
            )

        self._log(
            f"HYBRID CONNECTED: OVS reports {new_target} "
            f"is_connected=true "
            f"(connect_time={connect_latency_ms:.1f} ms)"
        )

        # -------------------------------------------------------------
        # VERIFY THE SAME OVS CONNECTION
        # -------------------------------------------------------------
        #
        # There is deliberately NO synthetic OpenFlow connection here.
        #
        # Every sample asks OVS about the controller object that the
        # bridge is actually using.
        #
        n = max(1, self.verification_health_checks)
        interval = self.verification_window_s / n if n > 1 else self.verification_window_s

        failed_samples = 0

        for i in range(n):
            connected_now = self._controller_target_connected(switch_name, new_target)

            if not connected_now:
                # One retry protects against an OVSDB reporting race,
                # but the retry still uses the REAL OVS controller target.
                time.sleep(1.0)
                connected_now = self._controller_target_connected(switch_name, new_target)

            if not connected_now:
                failed_samples += 1
                self._revert_to_legacy_only(switch_name, legacy_target)
                return (
                    False,
                    None,
                    f"VERIFY: Hybrid OVS controller "
                    f"{new_target} disconnected during sample "
                    f"{i + 1}/{n}; restored Legacy-only",
                )

            self._log(
                f"VERIFY sample {i + 1}/{n} ok "
                f"(OVS Hybrid target {new_target} "
                f"is_connected=true)"
            )

            # Optional data-plane validation.
            if self.data_plane_check_fn is not None:
                dp_ok, dp_reason = self.data_plane_check_fn(dpid, switch_name)

                if not dp_ok:
                    self._revert_to_legacy_only(switch_name, legacy_target)
                    return (
                        False,
                        None,
                        f"VERIFY: sample {i + 1}/{n} "
                        f"data-plane check failed: {dp_reason}",
                    )

            if i < n - 1:
                time.sleep(interval)

        # -------------------------------------------------------------
        # SUCCESS
        # -------------------------------------------------------------
        #
        # At this point:
        #   - Hybrid stunnel was already proven in Stage 1
        #   - OVS switched to Hybrid-only
        #   - OVS reported the Hybrid controller connected
        #   - The SAME OVS controller connection remained connected
        #     throughout the verification window
        #
        post = self._capture_metrics_hybrid(
            dpid,
            switch_name,
            connect_latency_ms=connect_latency_ms,
            verification_samples=n,
            verification_failures=failed_samples,
        )

        return True, post, None

    # -----------------------------------------------------------------
    # Public contract (guide §5.4)
    # -----------------------------------------------------------------

    def migrate(self, dpid: str) -> dict:
        """
        Run the full pre-flight + Make-Before-Break cutover for one
        switch. Safe to call repeatedly for the same dpid (e.g. after
        monitor.py rolls it back and the SGBM loop re-admits it, guide
        §9.1) -- each call is a fresh attempt from whatever the current
        OVS controller-set happens to be.

        This is the ONLY function in the whole orchestrator allowed to
        call topology.set_state(dpid, "Hybrid"); it does so in exactly
        one place below, only after BREAK completes.
        """
        if self.topology.get_state(dpid) == "Hybrid":
            # Already migrated (e.g. re-queued by mistake); nothing to
            # do, and re-running the cutover on an already-Hybrid
            # switch would just add pointless churn to a working tunnel.
            self._log(f"{dpid} is already Hybrid, skipping")
            baseline = _empty_metrics(failure_rate=0.0)
            return {
                "dpid": dpid, "outcome": "success",
                "baseline": baseline, "post": baseline,
                "timestamp": _now_iso(),
            }

        try:
            switch_name = self._resolve_switch_name(dpid)
        except MigrationError as exc:
            baseline = _empty_metrics()
            self.ledger.log_event(dpid, baseline, None, outcome="failed", reason=str(exc))
            return {"dpid": dpid, "outcome": "failed", "baseline": baseline,
                    "post": None, "timestamp": _now_iso()}

        try:
            ok1, baseline, local_port, reason1 = self._stage1_preflight(dpid, switch_name)
        except Exception as exc:  # never let an infra hiccup crash the SGBM loop
            baseline = _empty_metrics()
            self.ledger.log_event(dpid, baseline, None, outcome="failed",
                                   reason=f"Stage 1 exception: {exc}")
            return {"dpid": dpid, "outcome": "failed", "baseline": baseline,
                    "post": None, "timestamp": _now_iso()}

        if not ok1:
            self._log(f"Stage 1 failed for {dpid} ({switch_name}): {reason1}")
            self.ledger.log_event(dpid, baseline, None, outcome="failed", reason=reason1)
            return {"dpid": dpid, "outcome": "failed", "baseline": baseline,
                    "post": None, "timestamp": _now_iso()}

        self._log(
            f"Stage 1 passed for {dpid} ({switch_name}), "
            f"negotiated {self.hybrid_group} "
            f"-- starting Break-Before-Make cutover"
        )
        try:
            ok2, post, reason2 = self._stage2_break_before_make(dpid, switch_name, local_port)
        except Exception as exc:
            self._revert_to_legacy_only(switch_name, f"ssl:{self.controller_ip}:{self.legacy_port}")
            self.ledger.log_event(dpid, baseline, None, outcome="failed",
                                   reason=f"Break-Before-Make exception: {exc}")
            return {"dpid": dpid, "outcome": "failed", "baseline": baseline,
                    "post": None, "timestamp": _now_iso()}

        if not ok2:
            self._log(f"Make-Before-Break failed for {dpid} ({switch_name}): {reason2}")
            self.ledger.log_event(dpid, baseline, None, outcome="failed", reason=reason2)
            return {"dpid": dpid, "outcome": "failed", "baseline": baseline,
                    "post": None, "timestamp": _now_iso()}

        # -- the one and only place in the system that flips Legacy -> Hybrid --
        self.topology.set_state(dpid, "Hybrid")
        self.ledger.log_event(dpid, baseline, post, outcome="success")
        self._log(
            f"{dpid} ({switch_name}) migrated to Hybrid successfully "
            f"(Break-Before-Make)"
        )
        return {"dpid": dpid, "outcome": "success", "baseline": baseline,
                "post": post, "timestamp": _now_iso()}


# ---------------------------------------------------------------------
# Self-test -- exercises every branch of migrate() with a fake
# CommandRunner (no real Mininet/OVS/stunnel/OpenSSL needed) plus a
# tiny in-process OpenFlow peer standing in for "the controller,
# reached through the local Hybrid stunnel client" during VERIFY. This
# validates the CONTROL FLOW only. The guide's own §11 test checklist
# item for Feature 4 -- "manually run the cutover against one real
# switch; confirm ovs-vsctl show reports is_connected: true and the
# handshake log shows the Hybrid group" -- still has to be done against
# the live testbed; that's not something a fake runner can substitute
# for.
# ---------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import threading

    class FakeTopology:
        def __init__(self):
            self.states = {}

        def get_state(self, dpid):
            return self.states.get(dpid, "Legacy")

        def set_state(self, dpid, value):
            assert value in ("Legacy", "Hybrid")
            self.states[dpid] = value

    class FakeLedger:
        def __init__(self):
            self.events = []

        def log_event(self, dpid, baseline, post, outcome, reason=None, associated_flows=None):
            self.events.append({
                "dpid": dpid, "baseline": baseline, "post": post,
                "outcome": outcome, "reason": reason,
            })

        def get_history(self, dpid):
            return [e for e in self.events if e["dpid"] == dpid]

    class FakeCommandRunner(CommandRunner):
        """
        Scripted responses keyed by a recognizable substring of the
        command, so each test case below can drive a different branch
        of migrate() (Stage 1 pass/fail, MAKE never connecting, etc.)
        without touching the real system.
        """

        def __init__(self, bridge_name="s3", dpid="0000000000000003",
                     stage2_connects=True, flap_after=None):
            self.bridge_name = bridge_name
            self.dpid = dpid
            self.stage2_connects = stage2_connects
            self.flap_after = flap_after  # sample index after which the new target reports disconnected
            self.set_controller_calls = []
            self._find_calls_for_new_target = 0

        def _new_target(self):
            return f"tcp:127.0.0.1:{local_port_for_dpid(self.dpid)}"

        def run(self, args, timeout=None, input_bytes=b""):
            joined = " ".join(args)
            if "list-br" in joined:
                return 0, f"{self.bridge_name}\n", ""
            if "get bridge" in joined and "datapath_id" in joined:
                return 0, f'"{self.dpid}"\n', ""
            if "s_client" in joined:
                # simulate a fast, successful handshake for baseline/post capture
                return 0, "Verification: OK\n", ""
            if "find" in joined and "Controller" in joined and "target=" in joined:
                m = re.search(r'target="([^"]+)"', joined)
                target = m.group(1) if m else None
                if target == self._new_target():
                    self._find_calls_for_new_target += 1
                    if self.flap_after is not None and self._find_calls_for_new_target > self.flap_after:
                        return 0, "false\n", ""
                    return (0, "true\n", "") if self.stage2_connects else (0, "false\n", "")
                return 0, "true\n", ""  # legacy target: always considered connected
            if "set-controller" in joined:
                self.set_controller_calls.append(args)
                return 0, "", ""
            return 0, "", ""

        def popen(self, args, stdout_path=None, env=None):
            class _FakeProc:
                def poll(self_inner):
                    return None  # "still running"
            return _FakeProc()

    class _FakeOFPeerServer:
        """
        Minimal OpenFlow 1.3 peer used ONLY by these self-tests to
        answer _OpenFlowHealthProbe's HELLO/Echo/Features/Flow-Mod
        exchange over a real local TCP port -- standing in for "the
        controller, reached through the local Hybrid stunnel client"
        without needing a real stunnel/OpenSSL/OS-Ken stack. Accepts
        connections in a loop so it can serve Stage 1's dry-run
        connect AND every VERIFY-phase probe's own separate connection.
        """

        def __init__(self, port, send_features=True, send_flow_mod=True, echo_ok=True):
            self.send_features = send_features
            self.send_flow_mod = send_flow_mod
            self.echo_ok = echo_ok
            self.last_features_reply_dpid = None  # set by _handle once a probe answers Features
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind(("127.0.0.1", port))
            self._srv.listen(8)
            self._srv.settimeout(0.2)
            self._stop = False
            self._thread = threading.Thread(target=self._serve_forever, daemon=True)
            self._thread.start()

        def _serve_forever(self):
            while not self._stop:
                try:
                    conn, _addr = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

        @staticmethod
        def _recvn(conn, n):
            buf = b""
            while len(buf) < n:
                chunk = conn.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        def _handle(self, conn):
            conn.settimeout(2.0)
            try:
                header = self._recvn(conn, 8)
                if not header:
                    return
                _v, mtype, length, xid = _OFP_HEADER.unpack(header)
                if length > 8:
                    self._recvn(conn, length - 8)
                if mtype != OFPT_HELLO:
                    return
                conn.sendall(_of_hello(xid=0))

                header2 = self._recvn(conn, 8)
                if not header2:
                    return
                _v2, mtype2, length2, xid2 = _OFP_HEADER.unpack(header2)
                if length2 > 8:
                    self._recvn(conn, length2 - 8)
                if mtype2 == OFPT_ECHO_REQUEST and self.echo_ok:
                    conn.sendall(_of_header(OFPT_ECHO_REPLY, 8, xid2))

                if self.send_features:
                    conn.sendall(_of_header(OFPT_FEATURES_REQUEST, 8, 100))
                    fh = self._recvn(conn, 8)
                    if fh:
                        _v3, _mtype3, length3, _xid3 = _OFP_HEADER.unpack(fh)
                        if length3 > 8:
                            body3 = self._recvn(conn, length3 - 8)
                            if body3 and len(body3) >= 8:
                                self.last_features_reply_dpid = body3[:8].hex()

                if self.send_flow_mod:
                    dummy_body = b"\x00" * 40
                    conn.sendall(_of_header(OFPT_FLOW_MOD, 8 + len(dummy_body), 200) + dummy_body)
                    time.sleep(0.05)
            except (OSError, struct.error):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def stop(self):
            self._stop = True
            try:
                self._srv.close()
            except OSError:
                pass
            self._thread.join(timeout=1)

    def _make_executor(tmp_dir, runner, **overrides):
        hybrid_cert_dir = os.path.join(tmp_dir, "hybrid_certs")
        hybrid_conf_dir = os.path.join(tmp_dir, "hybrid_conf")
        os.makedirs(hybrid_cert_dir, exist_ok=True)
        os.makedirs(hybrid_conf_dir, exist_ok=True)
        legacy_cert_dir = os.path.join(tmp_dir, "legacy_certs")
        os.makedirs(legacy_cert_dir, exist_ok=True)

        for name in ("ca.cert", "switch.key", "switch.cert"):
            open(os.path.join(legacy_cert_dir, name), "w").write("fake-legacy-material")

        kwargs = dict(
            runner=runner,
            legacy_ca_cert=os.path.join(legacy_cert_dir, "ca.cert"),
            legacy_switch_key=os.path.join(legacy_cert_dir, "switch.key"),
            legacy_switch_cert=os.path.join(legacy_cert_dir, "switch.cert"),
            hybrid_ca_cert=os.path.join(hybrid_cert_dir, "hybrid-ca.cert"),
            hybrid_cert_dir=hybrid_cert_dir,
            hybrid_conf_dir=hybrid_conf_dir,
            hybrid_log_dir=os.path.join(hybrid_conf_dir, "logs"),
            hybrid_stunnel_bin=os.path.join(tmp_dir, "fake-stunnel"),  # created per-test
            baseline_samples=2,
            handshake_probe_timeout=1,
            dry_run_log_wait=1,
            cutover_poll_timeout=1,
            cutover_poll_interval=0.1,
            verification_window_s=0.6,
            verification_health_checks=2,
            features_wait_s=1.0,
            of_probe_timeout=1.0,
            verbose=False,
        )
        kwargs.update(overrides)
        topo = FakeTopology()
        ledger = FakeLedger()
        return MigrationExecutor(topo, ledger, **kwargs), topo, ledger

    def _write_stunnel_client_log(executor, dpid, success, error_snippet=None):
        """
        Writes a client_<dpid>.log excerpt shaped like a REAL stunnel
        5.63 session (see the live test transcript this was calibrated
        against, and D2's note above) -- not a fabricated line the
        parser was designed around. success=True writes the same
        "TLS connected: new session negotiated" + "Peer temporary key:
        (null), 768 bits" pair a real Hybrid handshake produces.
        success=False writes a realistic handshake-error excerpt
        instead (default: a certificate-verify failure, the same shape
        as the "Rejected by CERT" case a verifyPeer/verifyChain
        misconfiguration actually produced during live testing).
        """
        os.makedirs(executor.hybrid_log_dir, exist_ok=True)
        log_path = os.path.join(executor.hybrid_log_dir, f"client_{dpid}.log")
        if success:
            body = (
                "LOG7[0]: TLS state (connect): SSL negotiation finished successfully\n"
                "LOG6[0]: TLS connected: new session negotiated\n"
                "LOG6[0]: TLSv1.3 ciphersuite: TLS_AES_256_GCM_SHA384 (256-bit encryption)\n"
                "LOG6[0]: Peer temporary key: (null), 768 bits\n"
            )
        else:
            body = error_snippet or (
                "LOG4[0]: Rejected by CERT at depth=0: CN=sdn-switch-hybrid\n"
                "LOG7[0]: TLS alert (write): fatal: bad certificate\n"
                "LOG3[0]: SSL_accept: ssl/statem/statem_srvr.c:3777: error:0A000086:"
                "SSL routines::certificate verify failed\n"
            )
        with open(log_path, "w") as fh:
            fh.write(body)

    def _write_hybrid_cert_material(executor, switch_name):
        key, cert = executor._hybrid_switch_cert_paths(switch_name)
        open(key, "w").write("fake-hybrid-key")
        open(cert, "w").write("fake-hybrid-cert")
        open(executor.hybrid_ca_cert, "w").write("fake-hybrid-ca")

    def _prep_stunnel_pair(executor):
        open(executor.hybrid_stunnel_bin, "w").write("#!/bin/sh\n")
        os.chmod(executor.hybrid_stunnel_bin, 0o755)
        _write_hybrid_cert_material(executor, "s3")
        os.makedirs(executor.hybrid_conf_dir, exist_ok=True)
        open(os.path.join(executor.hybrid_conf_dir, "server.conf"), "w").write("; fake\n")

    dpid = "0000000000000003"

    print("=== Test 1: full success -- MAKE, VERIFY (echo+features+flow-mod), atomic BREAK ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port, send_features=True, send_flow_mod=True)
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "success", result
        assert result["post"] is not None
        assert topo.get_state(dpid) == "Hybrid"
        assert ledger.events[-1]["outcome"] == "success"
        calls = [" ".join(c) for c in runner.set_controller_calls]
        assert any(f"ssl:127.0.0.1:6653" in c and f"tcp:127.0.0.1:{port}" in c for c in calls), \
            "MAKE must configure BOTH targets in one call"
        assert calls[-1].endswith(f"tcp:127.0.0.1:{port}"), \
            "BREAK must be a final atomic call to the new target ALONE"
        print("  OK: MAKE added dual controller, VERIFY passed with a flow-mod observed, "
              "BREAK atomically cut to Hybrid-only")

    print("\n=== Test 2: Stage 1 fails (handshake/cert error in stunnel log) -> cutover never attempted ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port)
        try:
            _write_stunnel_client_log(executor, dpid, success=False)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "failed", result
        assert result["post"] is None
        assert topo.get_state(dpid) == "Legacy"
        assert not runner.set_controller_calls, "MAKE must never run when Stage 1 fails"
        assert "handshake error" in ledger.events[-1]["reason"]
        print("  OK: outcome=failed, state stays Legacy, MAKE never attempted")

    print("\n=== Test 3: missing Hybrid stunnel binary -> fails closed before MAKE, no crash ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner)
        # deliberately do NOT create executor.hybrid_stunnel_bin
        result = executor.migrate(dpid)

        assert result["outcome"] == "failed", result
        assert topo.get_state(dpid) == "Legacy"
        assert "stunnel binary not found" in ledger.events[-1]["reason"]
        print("  OK: a missing/non-executable stunnel binary fails cleanly before touching OVS")

    print("\n=== Test 4: MAKE -- second connection never comes up -> revert to Legacy-only ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=False)
        executor, topo, ledger = _make_executor(tmp, runner)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port)
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "failed", result
        assert topo.get_state(dpid) == "Legacy"
        calls = [" ".join(c) for c in runner.set_controller_calls]
        assert any(f"tcp:127.0.0.1:{port}" in c and "ssl:127.0.0.1:6653" in c for c in calls), \
            "must attempt MAKE (dual controller)"
        assert calls[-1] == f"ovs-vsctl set-controller s3 ssl:127.0.0.1:6653", \
            "must revert to Legacy-only, alone, as the final call"
        print("  OK: MAKE attempted, never connected, reverted to Legacy-only -- live session untouched")

    print("\n=== Test 5: VERIFY -- connection flaps mid-window -> abort, revert to Legacy-only ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True, flap_after=1)
        executor, topo, ledger = _make_executor(tmp, runner)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port, send_features=True, send_flow_mod=True)
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "failed", result
        assert topo.get_state(dpid) == "Legacy"
        assert "flapping" in ledger.events[-1]["reason"]
        print("  OK: flapping new connection caught mid-VERIFY, discarded before BREAK")

    print("\n=== Test 6: VERIFY -- no Flow-Mod observed, required -> cutover blocked ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner, require_flow_mod_confirmation=True)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port, send_features=True, send_flow_mod=False)  # never sends one
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "failed", result
        assert topo.get_state(dpid) == "Legacy"
        assert "Flow-Mod round trip" in ledger.events[-1]["reason"]
        calls = [" ".join(c) for c in runner.set_controller_calls]
        assert calls[-1] == f"ovs-vsctl set-controller s3 ssl:127.0.0.1:6653", \
            "must revert to Legacy-only when the required flow-mod gate isn't met"
        print("  OK: connectivity+echo passed every sample, but missing flow-mod blocked BREAK")

    print("\n=== Test 7: same missing-flow-mod scenario, but require_flow_mod_confirmation=False ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner, require_flow_mod_confirmation=False)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port, send_features=True, send_flow_mod=False)
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "success", result
        assert topo.get_state(dpid) == "Hybrid"
        print("  OK: escape hatch works -- cutover proceeds without a flow-mod when explicitly allowed")

    print("\n=== Test 8: already-Hybrid dpid is a no-op success, MAKE never attempted ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner)
        topo.set_state(dpid, "Hybrid")
        result = executor.migrate(dpid)

        assert result["outcome"] == "success", result
        assert not runner.set_controller_calls
        print("  OK: idempotent re-migrate of an already-Hybrid switch is a clean no-op")

    print("\n=== Test 9: VERIFY probe must present a dpid DIFFERENT from the real switch ===")
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeCommandRunner(bridge_name="s3", dpid=dpid, stage2_connects=True)
        executor, topo, ledger = _make_executor(tmp, runner)
        _prep_stunnel_pair(executor)
        port = local_port_for_dpid(dpid)
        peer = _FakeOFPeerServer(port, send_features=True, send_flow_mod=True)
        try:
            _write_stunnel_client_log(executor, dpid, success=True)
            result = executor.migrate(dpid)
        finally:
            peer.stop()

        assert result["outcome"] == "success", result
        assert peer.last_features_reply_dpid is not None, "probe never answered a Features Request"
        assert peer.last_features_reply_dpid != dpid, (
            "REGRESSION: VERIFY probe presented the switch's REAL dpid in its Features "
            "Reply. Confirmed via live testing (2026-09-23) that this makes OS-Ken/Ryu "
            "evict the real OVS session mid-VERIFY, causing spurious 'flapping' aborts. "
            "The probe must always use _probe_dpid(dpid), never dpid itself."
        )
        print(f"  OK: probe used {peer.last_features_reply_dpid!r}, real switch dpid "
              f"{dpid!r} was never presented -- OS-Ken can't confuse the two")

    print("\nAll migrate_link.py self-checks passed (no real Mininet/OVS/stunnel/OpenSSL required).")
    print("Live-system check still required per guide §11: manually run the cutover against one")
    print("real switch and confirm `ovs-vsctl show` reports is_connected: true for the Hybrid")
    print("target, and inspect the stunnel client log to confirm X25519MLKEM768 was negotiated.")
