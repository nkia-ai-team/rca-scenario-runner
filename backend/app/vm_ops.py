"""Management-network operations for the VM testbed (civil-petition).

testbed-services docs/spec-testbed-vm-3tier.md §6-2 (executor plumbing traces), §6-4 (preflight), §6-6 (the
runner's ssh common part). Nothing here changes a VM: every remote call is a read.

Always-open connection (§6-2, ssh row). Every login opens an sshd login line, a logind session, a session scope
and a ``user@<uid>`` unit on the VM, and a new row in the SMS connection sample (``host_connections``). So the
runner keeps one OpenSSH ControlMaster connection per VM, opened once and multiplexed by every later call: the
executors (testbed-services profiles) and this module's reads. The supervisor checks each master every
``MASTER_CHECK_INTERVAL_SEC`` with ``ssh -O check`` (a local socket call, nothing reaches the VM) and reopens a
dead one; the time it opened is what preflight's 30-minute settle reads (§6-4 "관리망 도달"), so the new
connection a reopen leaves in ``host_connections`` never sits at the front of a recording.

Contract with the executors (agreed with testbed-services T4S, both result files carry the same table): both
sides use ``ssh_options`` (``-F none``, the executor key, IdentitiesOnly, the domain's known_hosts file, the shared
ControlPath under ``CIVIL_PETITION_SSH_CONTROL_DIR``). The executors call with ``-o ControlMaster=no``: ssh
multiplexes over the socket when the master is alive and otherwise connects on its own. A T4S executor's ``run``
refuses when the master is down; its cleanup and recovery go anyway (that login is filtered by the VM's rsyslog
first rule). Only this module opens masters (``ControlMaster=yes``, ``ControlPersist=yes``, ``-N -f``).

Periodic read (§6-2, process sample row (3)). The SMS process sample (``process_snapshot``, every 60s) carries the
executor account's ``sshd``, ``sudo`` and ``bash`` whenever an executor call is running at sample time. That
cannot be filtered, so the same rows must appear at the same rate when nothing is injected: every
``PERIODIC_READ_INTERVAL_SEC`` the runner reads each VM through the same account and the same ``sudo -n bash -s``
form (service state, checksums, guard residue, clock offset, the router's CPU counters, the loadgen summary and
the db01 archive facts line). The read writes nothing on the VM and finishes well inside ``REMOTE_TIMEOUT_SEC``
(one SMS process sample is 60s). Its result is the input of the domain preflight (app.preflight_probe) and of the
archive-state readiness (app.live_queue ARCHIVE_STATE_*): the db01 line (pg-table-stats ``last.json``) is copied
as it is into ``PETITION_ARCHIVE_STATE_DIR/last.json``, a file read with no database session.

Hypervisor (66). The account there (``hvjob``) runs one forced command, the T4S helper ``t4-hv-exec``, which takes
its request on stdin's first line. The runner uses its three read verbs (``tunnel``, ``guards``, ``version``,
each one JSON line) with the periodic read and again right before an injection, and keeps one master to 66 as
well: the T4S 66 executors refuse to run without it (66's rsyslog filter keeps that account's lines away from 119,
§6-2 66 row, but not the nameless session scope lines a new login leaves), and preflight checks it with the six.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import random
import re
import shlex
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("scenario_runner.vm_ops")

DOMAIN = "civil-petition"
EXECUTOR_USER = "opsjob"
# Keys come in through one read-only directory mount (a single-file bind mount of a key that does not exist yet
# makes docker create a directory at the host path).
KEY_DIR = Path(os.environ.get("VM_OPS_KEY_DIR", "/root/.ssh/vm-ops"))
EXECUTOR_KEY = KEY_DIR / "t4-executor"
HYPERVISOR_KEY = KEY_DIR / "t4-hv"
KNOWN_HOSTS = KEY_DIR / "known_hosts_civil-petition"
# Shared with the executors (contract). ssh expands %r, %h and %p to user, host and port, so both sides name the
# same socket from the same target. Inside the runner container only.
CONTROL_DIR = Path(os.environ.get("CIVIL_PETITION_SSH_CONTROL_DIR", "/app/state/ssh-control"))
CONTROL_PATH = str(CONTROL_DIR / "%r@%h:%p")
CONNECT_TIMEOUT_SEC = 10
# One remote call stays well inside one SMS process-sample interval (60s, §6-2 (2)).
REMOTE_TIMEOUT_SEC = 10
# §6-2 ssh row: ServerAliveInterval 30s keeps the master's NAT entry on 66 and notices a dead peer.
SERVER_ALIVE_INTERVAL_SEC = 30
SERVER_ALIVE_COUNT_MAX = 3
MASTER_CHECK_INTERVAL_SEC = 30
MASTER_OPEN_WAIT_SEC = 15
# A master that could not open (the opening ssh failed by itself, nothing left on the VM) is tried again after this.
MASTER_RETRY_SEC = 300
STOP_LOOP_WAIT_SEC = 5
PERIODIC_READ_INTERVAL_SEC = 300
# A read result older than two periods plus a minute means the periodic read stopped (or a VM did not answer
# twice): preflight does not judge on it.
FACTS_MAX_AGE_SEC = 2 * PERIODIC_READ_INTERVAL_SEC + 60
# How many 66 counter samples to keep (the tunnel load is a 10-minute average over them).
HYPERVISOR_HISTORY = 12
ROUTER_CPU_HISTORY = 4
STATE_DIR = Path(os.environ.get("VM_OPS_STATE_DIR", "/app/state/vm-ops"))

# 66 as tb-ops reaches it (vmbr1), the helper account (T4S contract 6). Fixed, not a setting (review rv-t4r3 L2): the
# socket name comes from it, and the executors (testbed-services vm_executor_common) name the same target, so a
# runner-only change would split the sockets and every 66 executor run would be refused.
HYPERVISOR_TARGET = "hvjob@192.168.122.1"
# The helper is rendered from the inventory at apply time (no file with addresses lives in the repository), so the
# repository keeps its expected checksums in one file (T4S): {"files": {"t4-hv-exec": "<sha256>", ...}}, the same
# shape the helper's `version` verb prints. Read from the testbed-services deploy directory mounted into the runner.
DEPLOY_DIR = Path(os.environ.get("CIVIL_PETITION_DEPLOY_DIR", "/opt/lucida/civil-petition-deploy"))
HELPER_CHECKSUMS = DEPLOY_DIR / "hypervisor" / "t4-hv-exec.sha256.json"
HELPER_FILES = ("t4-hv-exec", "t4-hv-exec-root")


@dataclass(frozen=True)
class VmHost:
    """One VM of the domain as the runner reaches it (management address only, §3-5 rule 4)."""

    name: str
    address: str
    role: str
    units: tuple[str, ...]
    # (label, absolute path) read whole and carried back base64-encoded.
    files: tuple[tuple[str, str], ...] = ()
    user: str = EXECUTOR_USER
    key: Path = EXECUTOR_KEY
    # The domain's known_hosts file (each VM testbed has its own; ssh_options).
    known_hosts: Path = KNOWN_HOSTS


# Inventory civil-petition/deploy/inventory/t.json (hosts, management NIC addresses) and the role units the T2
# install starts. Fixed here like every other runner allowlist: a scenario cannot name another host.
LOADGEN_LIVE_PATH = f"/tmp/k6-{DOMAIN}-live.json"
ARCHIVE_FACTS_PATH = "/var/lib/pg-table-stats/last.json"
# Pair condition 1 (S design §2-4, §7-4; ST4 contract 3, round 2 after review rv-st4r M2): what every VM of both
# testbeds installed, read whole by the periodic read and compared with the one expected value in the repository
# (PAIR_EXPECTED): the WAR record (the WAS pair) and the normalized checksums of every rendered file, the load
# scripts on the loadgen included (record step: `<normalized_sha256>  <dest>`, addresses and names replaced by
# placeholders at render time; the loadgen adds `<sha256>  loadgen/script.js` and `loadgen/entrypoint.sh`). A VM
# without the file reads as «-» (no pair).
NORMALIZED_RECORD = "/var/lib/petition-deploy/normalized.sha256"
PAIR_FILES: tuple[tuple[str, str], ...] = (
    ("pair_war", "/var/lib/petition-deploy/war.sha256"),
    ("pair_normalized", NORMALIZED_RECORD),
)
# The one expected value both testbeds are held to, pair by pair (testbed-services civil-petition/deploy/inventory/
# pair-expected.json, made by render.py from the repository's templates and both inventories, next to the other
# site-valued files; read from the mounted deploy folder; ST4 contract M2).
PAIR_EXPECTED = DEPLOY_DIR / "inventory" / "pair-expected.json"
PAIR_ROLES = ("web", "was", "db", "loadgen")
PAIR_EXPECTED_SCHEMA = "petition-pair-expected/1"
# The units the install starts per role, the same on every VM testbed (one install code, S design decision 5).
ROLE_UNITS: dict[str, tuple[str, ...]] = {
    "router": ("snmpd.service", "nftables.service"),
    "web": ("apache2.service", "lucida-agent.service"),
    "was": ("tomcat10.service", "lucida-agent.service"),
    "db": ("postgresql@16-main.service", "lucida-agent.service"),
    "loadgen": ("loadgen.service",),
}
SERVER_ROLES = ("web", "was", "db")
HOSTS: tuple[VmHost, ...] = (
    VmHost("rt-core01", "192.168.127.2", "router", ROLE_UNITS["router"]),
    VmHost("web01", "192.168.127.21", "web", ROLE_UNITS["web"], PAIR_FILES),
    VmHost("was01", "192.168.127.31", "was", ROLE_UNITS["was"], PAIR_FILES),
    VmHost("was02", "192.168.127.32", "was", ROLE_UNITS["was"], PAIR_FILES),
    VmHost("db01", "192.168.127.41", "db", ROLE_UNITS["db"], (("archive_state", ARCHIVE_FACTS_PATH), *PAIR_FILES)),
    VmHost(
        "loadgen", "192.168.127.10", "loadgen", ROLE_UNITS["loadgen"], (("loadgen_live", LOADGEN_LIVE_PATH), *PAIR_FILES)
    ),
)
HOSTS_BY_NAME = {host.name: host for host in HOSTS}


def hypervisor_host(target: str = HYPERVISOR_TARGET) -> VmHost | None:
    """66 as a master target (no periodic VM read runs there: its reads are the helper verbs)."""
    if not target or "@" not in target:
        return None
    user, address = target.split("@", 1)
    return VmHost("pve66", address, "hypervisor", (), (), user, HYPERVISOR_KEY)


# The address each SMS agent reports as (LUCIDA_HOST, the business address; the 119 series label host_name) and the
# router's polled address (its NMS target is found by this address in the target table, §4).
SERVER_AGENT_HOSTS = {"web01": "192.168.124.11", "was01": "192.168.125.11", "was02": "192.168.125.12", "db01": "192.168.126.11"}
ROUTER_NAME = "rt-core01"
ROUTER_POLL_ADDRESS = "192.168.123.254"
# The portal's entry as the loadgen calls it (inventory: web01 business address, web_port 80, context petition).
ENTRY_URL = "http://192.168.124.11:80/petition"


@dataclass(frozen=True)
class VmDomain:
    """One VM testbed as the runner reaches it: its VMs, the names its SMS agents report as, its loadgen and entry,
    its own known_hosts and state, and whether it has 66 executors and a router (S design §7-6, ST4 contract 2)."""

    key: str
    hosts: tuple[VmHost, ...]
    agent_hosts: Mapping[str, str]
    entry_url: str
    loadgen_live_path: str
    hypervisor_target: str
    router: bool
    # Under STATE_DIR: "" keeps T where it always was; another domain gets its own folder.
    state_subdir: str
    # What the operator CLI's node list takes to name this domain (none for T, its default).
    cli_args: tuple[str, ...]

    def host(self, name: str) -> VmHost:
        for host in self.hosts:
            if host.name == name:
                return host
        raise VmOpsError(f"{name!r} is not a VM of {self.key}")

    def role_host(self, role: str) -> VmHost:
        for host in self.hosts:
            if host.role == role:
                return host
        raise VmOpsError(f"{self.key} has no {role} VM")

    @property
    def servers(self) -> tuple[VmHost, ...]:
        return tuple(host for host in self.hosts if host.role in SERVER_ROLES)


# The S testbed (testbed-services docs/spec-testbed-sms-only.md): the same app on five more VMs, SMS only, no router,
# no 66 executors. Its key is fixed here like the other domain keys (decoy domain words, preflight signal sets); the
# inventory must carry the same value or it is refused.
SMS_DOMAIN = "civil-petition-sms"
SMS_INVENTORY = DEPLOY_DIR / "inventory" / "s.json"


def load_inventory_domain(path: Path, *, key: str | None = None) -> VmDomain:
    """A VM testbed read from its inventory (testbed-services civil-petition/deploy/inventory/<t|s>.json), the one
    place its names and addresses live (ST4 contract 2: no copy here). The NIC on the network marked ``management``
    = the always-open connection; a server's ``gateway`` NIC = the address its SMS agent reports as (LUCIDA_HOST);
    the executor key is the inventory's executor public key file without ``.pub`` under KEY_DIR; known_hosts is
    ``known_hosts_<domain>``."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        domain = str(document["domain"])
        executor = document["accounts"]["executor"]
        user = str(executor["name"])
        key_name = Path(str(executor["pubkey_file"])).name
        app = document["app"]
        raw_hosts = list(document["hosts"])
        management = {name for name, net in (document.get("networks") or {}).items() if net.get("management") is True}
    except Exception as error:
        raise VmOpsError(f"inventory {path} unreadable: {type(error).__name__}: {error}") from None
    if key is not None and domain != key:
        raise VmOpsError(f"inventory {path} is for {domain!r}, not {key!r}")
    if not key_name.endswith(".pub"):
        raise VmOpsError(f"inventory {path}: executor pubkey_file {key_name!r} does not end in .pub")
    private_key = KEY_DIR / key_name[: -len(".pub")]
    known_hosts = KEY_DIR / f"known_hosts_{domain}"
    loadgen_live = f"/tmp/k6-{domain}-live.json"
    hosts: list[VmHost] = []
    agents: dict[str, str] = {}
    for raw in raw_hosts:
        name, role = str(raw["name"]), str(raw["role"])
        if role not in ROLE_UNITS:
            raise VmOpsError(f"inventory {path}: {name} has unknown role {role!r}")
        nics = list(raw.get("nics") or [])
        mgmt = [str(nic["addr"]) for nic in nics if nic.get("net") in management]
        other = [str(nic["addr"]) for nic in nics if nic.get("gateway") is True and nic.get("net") not in management]
        if len(mgmt) != 1:
            raise VmOpsError(f"inventory {path}: {name} has {len(mgmt)} management NICs, expected 1")
        files: tuple[tuple[str, str], ...] = ()
        if role in SERVER_ROLES:
            if not other:
                raise VmOpsError(f"inventory {path}: server {name} has no gateway NIC")
            agents[name] = other[0]
            files = PAIR_FILES
        if role == "db":
            files = (("archive_state", ARCHIVE_FACTS_PATH), *files)
        if role == "loadgen":
            files = (("loadgen_live", loadgen_live), *PAIR_FILES)
        hosts.append(VmHost(name, mgmt[0], role, ROLE_UNITS[role], files, user, private_key, known_hosts))
    names = [host.name for host in hosts]
    if len(names) != len(set(names)):
        raise VmOpsError(f"inventory {path}: host names repeat")
    webs = [host for host in hosts if host.role == "web"]
    if len(webs) != 1 or not [host for host in hosts if host.role == "loadgen"]:
        raise VmOpsError(f"inventory {path}: needs one web VM and a loadgen VM")
    entry = f"http://{agents[webs[0].name]}:{int(app['web_port'])}/{str(app['context']).strip('/')}"
    return VmDomain(
        key=domain, hosts=tuple(hosts), agent_hosts=agents, entry_url=entry, loadgen_live_path=loadgen_live,
        hypervisor_target="", router=any(host.role == "router" for host in hosts),
        state_subdir=domain, cli_args=("--domain", domain),
    )


T_DOMAIN = VmDomain(
    key=DOMAIN, hosts=HOSTS, agent_hosts=SERVER_AGENT_HOSTS, entry_url=ENTRY_URL, loadgen_live_path=LOADGEN_LIVE_PATH,
    hypervisor_target=HYPERVISOR_TARGET, router=True, state_subdir="", cli_args=(),
)
# Checksum records a role must have and pass (the WAR on the WAS pair; the install's file list on every VM).
REQUIRED_RECORDS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "was": ("/var/lib/petition-deploy/war.sha256",),
}
REQUIRED_RECORDS_ALL: tuple[str, ...] = ("/var/lib/petition-deploy/expected.sha256",)

# What the periodic read compares on the VM (the install leaves both records; T4S contract item 6).
# war.sha256 names the WAR relative to the Tomcat webapps directory, expected.sha256 names absolute paths.
CHECKSUM_RECORDS = (
    ("/var/lib/petition-deploy/war.sha256", "/var/lib/tomcat10/webapps"),
    ("/var/lib/petition-deploy/expected.sha256", "/"),
)
CHECKSUM_RECORD_NAMES = frozenset(record for record, _ in CHECKSUM_RECORDS)
# The executor guard's state root (testbed-services executor_common GUARD_FUNCTIONS: <job>.unit while armed;
# T4S contract 8: no *.unit and no run-r* timer = no residue).
GUARD_STATE_ROOT = "/var/lib/opsjobs"

# The read body. The runner sends `set -- <units> -- <label=path>...` in front of it on stdin, so the node's auth
# log only ever records `bash -s` (and the VM's rsyslog first rule keeps that line local anyway). Pure bash and
# coreutils: no interpreter process of its own shows up in a process sample. It writes nothing.
READ_BODY = r'''units=() files=()
while (($#)); do if [[ "$1" == -- ]]; then shift; break; fi; units+=("$1"); shift; done
files=("$@")
echo "@@ at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "@@ host $(hostname)"
for u in "${units[@]}"; do echo "@@ unit $u $(systemctl is-active "$u" 2>/dev/null || true)"; done
check_sums() {
  local record=$1 base=$2 out rc
  if [[ ! -r "$record" ]]; then echo "@@ sums $record missing"; return; fi
  out=$(cd "$base" && sha256sum -c --quiet "$record" 2>/dev/null) && rc=0 || rc=$?
  echo "@@ sums $record rc=$rc"
  while IFS= read -r line; do [[ -n "$line" ]] && echo "@@ drift $record ${line%%: *}"; done <<<"$out"
}
'''.replace("\r", "") + "".join(
    f"check_sums {shlex.quote(record)} {shlex.quote(base)}\n" for record, base in CHECKSUM_RECORDS
) + r'''undo=$(find ''' + GUARD_STATE_ROOT + r''' -maxdepth 1 -name '*.unit' 2>/dev/null | wc -l)
timers=$(systemctl list-timers --all --no-legend 'run-r*' 2>/dev/null | wc -l)
echo "@@ guards $undo $timers"
echo "@@ chrony $(chronyc -c tracking 2>/dev/null | head -1)"
echo "@@ procstat $(head -1 /proc/stat)"
for spec in "${files[@]}"; do
  label=${spec%%=*} path=${spec#*=}
  if [[ -r "$path" ]]; then echo "@@ file $label $(base64 -w0 <"$path")"; else echo "@@ file $label -"; fi
done
echo "@@ end"
'''


def _drift_absolute(path: str) -> str:
    """A drift line as an absolute path: expected.sha256 names absolute paths, war.sha256 names the WAR relative to
    its base (CHECKSUM_RECORDS)."""
    return path if path.startswith("/") else f"{CHECKSUM_RECORDS[0][1]}/{path}"


class VmOpsError(RuntimeError):
    """A management-network read failed; the message carries the remote's own words (U3)."""


ProcessRunner = Callable[..., subprocess.CompletedProcess]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise VmOpsError(f"bad timestamp {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise VmOpsError(f"timestamp without zone {value!r}")
    return parsed.astimezone(timezone.utc)


def _tail(text: str, limit: int = 300) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:limit] if lines else "no output"


def _environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"), "HOME": "/root", "LC_ALL": "C"}


def ssh_options(*, key: Path = EXECUTOR_KEY, known_hosts: Path = KNOWN_HOSTS) -> list[str]:
    """The common options both sides use (T4S contract 3)."""
    return [
        "-F", "none",
        "-i", str(key),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SEC}",
        "-o", f"ControlPath={CONTROL_PATH}",
    ]


def executor_argv(host: VmHost) -> list[str]:
    """The executor-call form over the shared socket (the same argv the testbed-services executors build)."""
    return [
        "/usr/bin/ssh", *ssh_options(key=host.key, known_hosts=host.known_hosts),
        "-o", "ControlMaster=no",
        f"{host.user}@{host.address}",
        "sudo", "-n", "bash", "-s",
    ]


def master_argv(host: VmHost) -> list[str]:
    return [
        "/usr/bin/ssh", *ssh_options(key=host.key, known_hosts=host.known_hosts),
        "-o", "ControlMaster=yes",
        "-o", "ControlPersist=yes",
        "-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_SEC}",
        "-o", f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        "-N", "-f",
        f"{host.user}@{host.address}",
    ]


def master_check_argv(host: VmHost) -> list[str]:
    return ["/usr/bin/ssh", *ssh_options(key=host.key, known_hosts=host.known_hosts), "-O", "check", f"{host.user}@{host.address}"]


def master_is_up(host: VmHost, process_runner: ProcessRunner = subprocess.run) -> bool | None:
    """``ssh -O check`` on the host's socket (local, nothing reaches the host): True up, False down, None unknown
    (the check itself timed out or could not run)."""
    try:
        result = process_runner(
            master_check_argv(host), capture_output=True, text=True, timeout=REMOTE_TIMEOUT_SEC,
            env=_environment(), check=False,
        )
    except Exception:
        return None
    return result.returncode == 0


def require_master(host: VmHost, process_runner: ProcessRunner = subprocess.run) -> None:
    """The runner's own reads go only over a live master (review rv-t4r3 R-M1). ControlMaster=no would otherwise
    log in directly, a new login and a new host_connections row (Q7c) every 5 minutes while the master is down; the
    read fails instead, the facts go stale and preflight stops the domain."""
    if master_is_up(host, process_runner) is not True:
        raise VmOpsError(f"control connection to {host.address} is down; not reading {host.name} without it")


def master_exit_argv(host: VmHost) -> list[str]:
    return ["/usr/bin/ssh", *ssh_options(key=host.key, known_hosts=host.known_hosts), "-O", "exit", f"{host.user}@{host.address}"]


def read_stdin(host: VmHost) -> bytes:
    arguments = [*host.units, "--", *(f"{label}={path}" for label, path in host.files)]
    return ("set -- " + " ".join(shlex.quote(item) for item in arguments) + "\n" + READ_BODY).encode()


@dataclass
class HostFacts:
    host: str
    at: str
    hostname: str
    units: dict[str, str] = field(default_factory=dict)
    sums: dict[str, str] = field(default_factory=dict)
    drift: list[str] = field(default_factory=list)
    # The same lines by the record that named them (review rv-t4u4 L-D: a record's rc=1 is a held decoy's doing
    # only when every line of that record is).
    drift_records: dict[str, list[str]] = field(default_factory=dict)
    guards_armed: int = 0
    guard_timers: int = 0
    chrony: str = ""
    procstat: list[int] = field(default_factory=list)
    files: dict[str, str | None] = field(default_factory=dict)


_LINE = re.compile(r"^@@ (\S+)(?: (.*))?$")


def parse_read(host: VmHost, text: str) -> HostFacts:
    """Parse one periodic read. A read that did not reach its end marker is a failed read, not partial facts."""
    values: dict[str, Any] = {"units": {}, "sums": {}, "drift": [], "drift_records": {}, "files": {}}
    ended = False
    at = hostname = ""
    for raw in text.splitlines():
        match = _LINE.match(raw.rstrip())
        if not match:
            continue
        key, rest = match.group(1), match.group(2) or ""
        if key == "at":
            at = rest.strip()
        elif key == "host":
            hostname = rest.strip()
        elif key == "unit":
            unit, _, state = rest.partition(" ")
            values["units"][unit] = state.strip() or "unknown"
        elif key == "sums":
            record, _, verdict = rest.partition(" ")
            values["sums"][record] = verdict.strip()
        elif key == "drift":
            # `@@ drift <record> <path>`: the record is one of CHECKSUM_RECORDS.
            record, _, path = rest.strip().partition(" ")
            if record not in CHECKSUM_RECORD_NAMES or not path.strip():
                raise VmOpsError(f"{host.name}: unexpected drift line {rest[:120]!r}")
            values["drift"].append(path.strip())
            values["drift_records"].setdefault(record, []).append(path.strip())
        elif key == "guards":
            parts = rest.split()
            values["guards"] = (int(parts[0]), int(parts[1]))
        elif key == "chrony":
            values["chrony"] = rest.strip()
        elif key == "procstat":
            parts = rest.split()
            if not parts or parts[0] != "cpu":
                raise VmOpsError(f"{host.name}: unexpected /proc/stat line {rest[:80]!r}")
            values["procstat"] = [int(item) for item in parts[1:]]
        elif key == "file":
            label, _, payload = rest.partition(" ")
            payload = payload.strip()
            values["files"][label] = None if payload in ("", "-") else base64.b64decode(payload).decode("utf-8")
        elif key == "end":
            ended = True
    if not ended:
        raise VmOpsError(f"{host.name}: the read did not finish: {_tail(text)}")
    if hostname != host.name:
        raise VmOpsError(f"{host.name}: answered as {hostname!r} (wrong VM behind {host.address})")
    _parse(at)
    guards = values.get("guards", (0, 0))
    return HostFacts(
        host=host.name,
        at=at,
        hostname=hostname,
        units=values["units"],
        sums=values["sums"],
        drift=values["drift"],
        drift_records=values["drift_records"],
        guards_armed=guards[0],
        guard_timers=guards[1],
        chrony=values.get("chrony", ""),
        procstat=values.get("procstat", []),
        files=values["files"],
    )


def _handshake_age(value: Any) -> int | None:
    """Seconds since the last WireGuard handshake, None when there was none (the helper's -1, null, negatives)."""
    if value is None or isinstance(value, bool):
        return None
    age = int(value)
    return None if age < 0 else age


def chrony_offset_ms(line: str) -> float:
    """|System time| from ``chronyc -c tracking`` (field 5, seconds) in ms; raises when not synchronised."""
    fields = line.split(",")
    if len(fields) < 14:
        raise VmOpsError(f"chronyc tracking unreadable: {line[:120]!r}")
    if fields[13].strip() not in {"Normal"}:
        raise VmOpsError(f"chrony not synchronised (leap status {fields[13].strip()!r})")
    return abs(float(fields[4])) * 1000.0


def cpu_busy_percent(older: Sequence[int], newer: Sequence[int]) -> float:
    """Average CPU busy share between two ``/proc/stat`` ``cpu`` lines (idle and iowait count as not busy)."""
    if len(older) < 8 or len(newer) < 8:
        raise VmOpsError("short /proc/stat sample")
    # user nice system idle iowait irq softirq steal; guest and guest_nice are already inside user and nice.
    older, newer = older[:8], newer[:8]
    total = sum(newer) - sum(older)
    idle = (newer[3] + newer[4]) - (older[3] + older[4])
    if total <= 0 or idle < 0:
        raise VmOpsError("CPU counters went backwards (the router restarted between reads)")
    return 100.0 * (total - idle) / total


def kill_process_group(pid: int, *, grace_sec: float = 5.0, leader: Any = None) -> None:
    """End a process started with start_new_session=True and everything in its group (an ssh it started included):
    SIGTERM, then SIGKILL after ``grace_sec`` to whatever is left. Only that group, never a pattern.

    ``leader`` (a Popen) is reaped while waiting: an unreaped leader is a zombie that keeps the group answering, so
    the wait always ran the full grace (review rv-t4o2 N4). Blocking: call it from a worker thread inside a loop
    (``asyncio.to_thread``), where the loop's own child watcher reaps an asyncio process meanwhile."""
    import signal
    import time

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_sec
    if leader is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            leader.wait(timeout=grace_sec)
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


# After the group is ended, how long to wait for the output pipes to close. A process that left the group (the CLI
# starts its ssh in a session of its own) may still hold them; the runner then stops reading instead of waiting for
# it (review rv-t4o2 N3). The CLI ends that ssh within its own 90-second limit.
PIPE_DRAIN_SEC = 3


def run_in_group(argv: Sequence[str], *, timeout: float, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """subprocess.run that, on timeout, ends the whole process group it started (review rv-t4o M2: killing only the
    CLI left its ssh behind, a session that never closes on the VM and keeps the VM's operator lock), then waits at
    most PIPE_DRAIN_SEC for the pipes and closes them (N3: a grandchild in another session that kept the pipes made
    this wait as long as that grandchild lived). Raises subprocess.TimeoutExpired after that."""
    process = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(env),
        start_new_session=True, close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(process.pid, leader=process)
        try:
            process.communicate(timeout=PIPE_DRAIN_SEC)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                with contextlib.suppress(Exception):
                    stream.close()
            process.wait()
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _atomic_write(path: Path, text: str, *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MasterSupervisor:
    """One ControlMaster per VM, reopened when it dies; remembers when each was opened."""

    def __init__(
        self,
        hosts: Sequence[VmHost] = HOSTS,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        process_runner: ProcessRunner = subprocess.run,
        clock: Clock = _utc_now,
        state_dir: Path = STATE_DIR,
        control_dir: Path = CONTROL_DIR,
        open_wait_sec: float = MASTER_OPEN_WAIT_SEC,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.hosts = tuple(hosts)
        self.popen = popen
        self.process_runner = process_runner
        self.clock = clock
        self.state_dir = state_dir
        self.control_dir = control_dir
        self.open_wait_sec = open_wait_sec
        self._sleep = sleep or (lambda seconds: threading.Event().wait(seconds))
        self._processes: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.status: dict[str, dict[str, Any]] = {
            host.name: {"alive": False, "opened_at": None, "detail": "not opened yet"} for host in self.hosts
        }

    def _alive(self, host: VmHost) -> bool:
        return master_is_up(host, self.process_runner) is True

    def socket_path(self, host: VmHost) -> Path:
        """The file ssh makes from CONTROL_PATH (%r@%h:%p) for this target (port 22)."""
        return self.control_dir / f"{host.user}@{host.address}:22"

    def _open(self, host: VmHost) -> None:
        """Open one master and prove it is there (review rv-t4r3 H1).

        A master that died without cleaning up (SIGKILL, OOM, a forced container stop) leaves its socket file on
        the state volume. ``ControlMaster=yes`` does not reuse it: listen fails with EEXIST, ssh says "already
        exists, disabling multiplexing" and goes on as a plain connection that ``-f`` detaches (pid unknown, never
        ended, a new login and a new host_connections row each time). So a dead socket file is removed first, and
        an open that leaves no working master is classified:
        - the opening ssh failed by itself (non-zero exit, or no answer and ended by us before it forked): nothing
          was left on the VM, so it is tried again after MASTER_RETRY_SEC (a VM rebooting, a network blip);
        - it exited 0 (it forked) but no master answers: a connection was left behind that the runner cannot see
          or end. It is not tried again until the runner restarts; the status says so and preflight stays dirty.
        """
        previous = self._processes.pop(host.name, None)
        if previous is not None and previous.poll() is None:
            previous.terminate()
        self.control_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.control_dir, 0o700)
        socket = self.socket_path(host)
        if socket.exists() or socket.is_symlink():
            socket.unlink()
            logger.warning("removed the dead control socket of %s (%s)", host.name, socket)
        log_path = self.state_dir / f"{host.name}.master.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as log:
            process = self.popen(
                master_argv(host), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                env=_environment(), close_fds=True, start_new_session=True,
            )
        self._processes[host.name] = process
        waited = 0.0
        while waited < self.open_wait_sec:
            if self._alive(host):
                self.status[host.name] = {"alive": True, "opened_at": _format(self.clock()), "detail": ""}
                logger.warning("management ssh to %s (%s) opened", host.name, host.address)
                return
            # With -f the first process exits 0 once it has authenticated and forked the master; anything else
            # that ends it is a failure to open.
            code = process.poll()
            if code is not None and code != 0:
                break
            self._sleep(1.0)
            waited += 1.0
        detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        code = process.poll()
        if code is None:
            process.terminate()  # still ours, before any fork: nothing is left
        self._processes.pop(host.name, None)
        said = _tail(detail) if detail.strip() else "no answer within the wait"
        if code == 0:
            self.status[host.name] = {
                "alive": False, "opened_at": None, "given_up": True,
                "detail": (
                    f"opened without a working master ({said}); a connection may be left on the VM, so the runner "
                    "does not try again until it restarts (end any stray ssh to this address on tb-ops first)"
                ),
            }
        else:
            self.status[host.name] = {
                "alive": False, "opened_at": None,
                "retry_at": _format(self.clock() + timedelta(seconds=MASTER_RETRY_SEC)),
                "detail": f"could not open: {said}",
            }
        logger.warning("management ssh to %s (%s) failed: %s", host.name, host.address, self.status[host.name]["detail"])

    def ensure(self) -> dict[str, dict[str, Any]]:
        """Check every master and reopen the dead ones. Returns the status (also written to masters.json)."""
        with self._lock:
            for host in self.hosts:
                state = master_is_up(host, self.process_runner)
                if state is None:
                    # The check itself timed out: the master may be alive, so its socket must not be removed and
                    # reopened over (review rv-t4r3 R-L1). Look again on the next round.
                    continue
                if state:
                    current = self.status.get(host.name) or {}
                    if current.get("opened_at") is None:
                        # A live socket this process did not open (left by an earlier runner process in the same
                        # container): its open time is unknown, so the 30-minute settle starts now.
                        current = {"alive": True, "opened_at": _format(self.clock()), "detail": "found open"}
                    self.status[host.name] = {
                        "alive": True, "opened_at": current["opened_at"], "detail": current.get("detail", ""),
                    }
                    continue
                current = self.status.get(host.name) or {}
                if current.get("given_up"):
                    continue
                retry_at = current.get("retry_at")
                if retry_at and self.clock() < _parse(retry_at):
                    continue
                self._open(host)
            snapshot = {name: dict(value) for name, value in self.status.items()}
        try:
            _atomic_write(self.state_dir / "masters.json", json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        except OSError as error:
            logger.warning("cannot record master status: %s", error)
        return snapshot

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: dict(value) for name, value in self.status.items()}

    def stop(self) -> None:
        """End the masters (``ssh -O exit`` on each socket: with -f the master is not our child) and any opening
        process still running. Never a pattern kill."""
        with self._lock:
            for host in self.hosts:
                process = self._processes.pop(host.name, None)
                if process is not None and process.poll() is None:
                    process.terminate()
                try:
                    self.process_runner(
                        master_exit_argv(host), capture_output=True, text=True, timeout=REMOTE_TIMEOUT_SEC,
                        env=_environment(), check=False,
                    )
                except Exception as error:
                    logger.warning("could not end the master to %s: %s", host.name, error)
                self.status[host.name] = {"alive": False, "opened_at": None, "detail": "stopped"}


class VmOps:
    """Reads over the management network and the facts they leave (state_dir/facts.json)."""

    def __init__(
        self,
        hosts: Sequence[VmHost] | None = None,
        *,
        domain: VmDomain = T_DOMAIN,
        process_runner: ProcessRunner = subprocess.run,
        clock: Clock = _utc_now,
        state_dir: Path | None = None,
        archive_state_dir: Path | None = None,
        hypervisor_target: str | None = None,
        supervisor: MasterSupervisor | None = None,
        routine: OperatorRoutine | None = None,
    ) -> None:
        self.domain = domain
        hosts = domain.hosts if hosts is None else hosts
        hypervisor_target = domain.hypervisor_target if hypervisor_target is None else hypervisor_target
        if state_dir is None:
            state_dir = STATE_DIR / domain.state_subdir if domain.state_subdir else STATE_DIR
        self.hosts = tuple(hosts)
        self.routine = routine or OperatorRoutine(clock=clock, state_dir=state_dir, cli_args=domain.cli_args)
        self.process_runner = process_runner
        self.clock = clock
        self.state_dir = state_dir
        self.archive_state_dir = archive_state_dir
        self.hypervisor_target = hypervisor_target
        hypervisor = hypervisor_host(hypervisor_target)
        self.supervisor = supervisor or MasterSupervisor(
            (*hosts, *([hypervisor] if hypervisor else [])),
            process_runner=process_runner, clock=clock, state_dir=state_dir,
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ remote calls

    def _remote_read(self, host: VmHost, stdin: bytes) -> str:
        require_master(host, self.process_runner)
        try:
            result = self.process_runner(
                executor_argv(host), input=stdin, capture_output=True, timeout=REMOTE_TIMEOUT_SEC,
                env=_environment(), check=False,
            )
        except subprocess.TimeoutExpired:
            raise VmOpsError(f"{host.name}: no answer within {REMOTE_TIMEOUT_SEC}s") from None
        stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        if result.returncode != 0:
            raise VmOpsError(f"{host.name}: read exited {result.returncode}: {_tail(stderr or stdout)}")
        return stdout

    def read_host(self, host: VmHost) -> HostFacts:
        return parse_read(host, self._remote_read(host, read_stdin(host)))

    def read_loadgen_live(self) -> tuple[bool, dict[str, Any], str]:
        """(loadgen unit active, live summary document, raw unit environment line) read now.

        The loadgen sends nothing to 119 and is not an SMS target (§2-4), so a fresh read per preflight leaves no
        trace in a recording; it still goes over the same socket and the same form."""
        host = next((item for item in self.hosts if item.role == "loadgen"), None)
        if host is None:
            raise VmOpsError(f"{self.domain.key} has no loadgen VM")
        live_path = dict(host.files).get("loadgen_live", self.domain.loadgen_live_path)
        body = (
            "echo \"@@ host $(hostname)\"\n"
            "echo \"@@ unit loadgen.service $(systemctl is-active loadgen.service 2>/dev/null || true)\"\n"
            "echo \"@@ env $(systemctl show -p Environment --value loadgen.service 2>/dev/null)\"\n"
            f"if [[ -r {live_path} ]]; then echo \"@@ file loadgen_live $(base64 -w0 <{live_path})\"; "
            "else echo '@@ file loadgen_live -'; fi\n"
            "echo '@@ end'\n"
        ).encode()
        text = self._remote_read(host, body)
        if "@@ end" not in text:
            raise VmOpsError(f"loadgen: the read did not finish: {_tail(text)}")
        state = env = payload = ""
        for line in text.splitlines():
            if line.startswith("@@ unit "):
                state = line.split(" ", 3)[3] if line.count(" ") >= 3 else ""
            elif line.startswith("@@ env "):
                env = line[len("@@ env "):]
            elif line.startswith("@@ file loadgen_live "):
                payload = line[len("@@ file loadgen_live "):].strip()
        if payload in ("", "-"):
            raise VmOpsError(f"loadgen: no live summary at {live_path} (unit {state or 'unknown'})")
        document = json.loads(base64.b64decode(payload).decode("utf-8"))
        if not isinstance(document, dict):
            raise VmOpsError("loadgen: live summary is not an object")
        return state.strip() == "active", document, env

    def _helper(self, verb: str) -> dict[str, Any]:
        """One read verb of the 66 helper (T4S contract 6, 7): the request on stdin's first line, one JSON line back,
        exit 3 refused, 1 failed."""
        hypervisor = hypervisor_host(self.hypervisor_target)
        if hypervisor is None:
            raise VmOpsError("hypervisor target is not configured")
        require_master(hypervisor, self.process_runner)
        argv = ["/usr/bin/ssh", *ssh_options(key=HYPERVISOR_KEY), "-o", "ControlMaster=no", self.hypervisor_target]
        try:
            result = self.process_runner(
                argv, input=f"{verb}\n", capture_output=True, text=True, timeout=REMOTE_TIMEOUT_SEC + 20,
                env=_environment(), check=False,
            )
        except subprocess.TimeoutExpired:
            raise VmOpsError(f"hypervisor: no answer to {verb}") from None
        if result.returncode != 0:
            what = "refused" if result.returncode == 3 else f"exited {result.returncode}"
            raise VmOpsError(f"hypervisor: {verb} {what}: {_tail(result.stderr or result.stdout)}")
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        if len(lines) != 1:
            raise VmOpsError(f"hypervisor: {verb} is not one line: {_tail(result.stdout)}")
        document = json.loads(lines[0])
        if not isinstance(document, dict):
            raise VmOpsError(f"hypervisor: {verb} is not an object")
        return document

    def hypervisor_facts(self) -> dict[str, Any]:
        """66's tunnel, guards and installed helper checksums read now, recorded in the 66 history.

        Kept in one record: at, handshake_age_sec (None = never), ping {sent, received, max_ms}, wg_rx_bytes,
        wg_tx_bytes, guards_active, helper_sha256 {file: hex}."""
        if not self.hypervisor_target:
            raise VmOpsError("hypervisor target is not configured")
        tunnel = self._helper("tunnel")
        guards = self._helper("guards")
        version = self._helper("version")
        try:
            ping = tunnel["ping"]
            sent, lost = int(ping["sent"]), int(ping["lost"])
            record = {
                "at": tunnel["at"],
                # The helper says -1 when there was never a handshake (t4-hv-exec-root); None and any negative
                # value mean «none» (review rv-t4r3 M1).
                "handshake_age_sec": _handshake_age(tunnel["handshake_age_s"]),
                "ping": {"sent": sent, "received": sent - lost, "max_ms": ping.get("max_ms")},
                "wg_rx_bytes": int(tunnel["wg_rx_bytes"]),
                "wg_tx_bytes": int(tunnel["wg_tx_bytes"]),
                "guards_active": int(guards["armed"]),
                "helper_sha256": dict(version["files"]),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise VmOpsError(f"hypervisor: answer lacks {error}") from None
        _parse(record["at"])
        self._record_hypervisor(record)
        return record

    # ------------------------------------------------------------------ state

    @property
    def facts_path(self) -> Path:
        return self.state_dir / "facts.json"

    def load(self) -> dict[str, Any]:
        path = self.facts_path
        if not path.is_file():
            return {"hosts": {}, "hypervisor": {"history": []}}
        document = json.loads(path.read_text(encoding="utf-8"))
        document.setdefault("hosts", {})
        document.setdefault("hypervisor", {"history": []})
        return document

    def _save(self, document: Mapping[str, Any]) -> None:
        _atomic_write(self.facts_path, json.dumps(document, indent=2, sort_keys=True) + "\n")

    def _record_hypervisor(self, sample: Mapping[str, Any]) -> None:
        with self._lock:
            document = self.load()
            history = list(document["hypervisor"].get("history", []))
            history.append(dict(sample))
            document["hypervisor"] = {
                "history": history[-HYPERVISOR_HISTORY:],
                "last_error": None,
                "read_at": _format(self.clock()),
            }
            self._save(document)

    def read_all(self) -> dict[str, Any]:
        """The periodic read: every VM, then 66. One failure does not stop the others; it is recorded as is."""
        results: dict[str, HostFacts | Exception] = {}
        for host in self.hosts:
            try:
                results[host.name] = self.read_host(host)
            except Exception as error:
                results[host.name] = error
        hypervisor_error: str | None = None
        if self.hypervisor_target:
            try:
                self.hypervisor_facts()
            except Exception as error:
                hypervisor_error = f"{type(error).__name__}: {error}"
        now = _format(self.clock())
        with self._lock:
            document = self.load()
            for name, result in results.items():
                entry = dict(document["hosts"].get(name) or {})
                if isinstance(result, Exception):
                    entry["last_error"] = {"at": now, "detail": f"{type(result).__name__}: {result}"[:600]}
                else:
                    cpu = list(entry.get("cpu_history") or [])
                    if result.procstat:
                        cpu.append({"at": result.at, "procstat": result.procstat})
                    entry.update(
                        {
                            "facts": {
                                "at": result.at,
                                "read_at": now,
                                "units": result.units,
                                "sums": result.sums,
                                "drift": result.drift,
                                "drift_records": result.drift_records,
                                "guards_armed": result.guards_armed,
                                "guard_timers": result.guard_timers,
                                "chrony": result.chrony,
                                # Pair condition 1 records as read (None = the file is not there).
                                "pair": {label: result.files.get(label) for label, _ in PAIR_FILES},
                            },
                            "cpu_history": cpu[-ROUTER_CPU_HISTORY:],
                            "last_error": None,
                        }
                    )
                    self._deliver_files(name, result)
                document["hosts"][name] = entry
            if hypervisor_error is not None:
                document["hypervisor"]["last_error"] = {"at": now, "detail": hypervisor_error[:600]}
            self._save(document)
        return document

    def expected_helper_sha256(self) -> dict[str, str]:
        """The 66 helper checksums the repository expects (HELPER_CHECKSUMS), compared as JSON values."""
        try:
            document = json.loads(HELPER_CHECKSUMS.read_text(encoding="utf-8"))
            files = document["files"]
        except Exception as error:
            raise VmOpsError(f"helper checksums unreadable at {HELPER_CHECKSUMS}: {type(error).__name__}: {error}") from None
        missing = [name for name in HELPER_FILES if not isinstance(files.get(name), str)]
        if missing:
            raise VmOpsError(f"helper checksums at {HELPER_CHECKSUMS} lack {missing}")
        return {name: files[name] for name in HELPER_FILES}

    def drift_detail(self, excused: Mapping[str, Mapping[str, Any]] | None = None) -> str | None:
        """What differs from the install on the VMs and 66, or None (live_queue VM_DOMAIN_STATE_CHECK_NAME).

        ``excused`` is decoys.held_excuses of the cycle: a file a VM decoy held through the recording changed
        (D1 (b)) is no drift, on that node only. Judged on the latest periodic read. A host whose read is missing or stale is left to preflight, which
        refuses to judge on stale facts and waits (then pauses) with the read error."""
        document = self.load()
        now = self.clock()
        problems: list[str] = []
        for host in self.hosts:
            facts = (document["hosts"].get(host.name) or {}).get("facts")
            if facts is None or (now - _parse(facts["read_at"])).total_seconds() > FACTS_MAX_AGE_SEC:
                continue
            problems.extend(host_drift(host, facts, excused))
        history = document["hypervisor"].get("history") or []
        if self.hypervisor_target and history:
            latest = history[-1]
            if (now - _parse(latest["at"])).total_seconds() <= FACTS_MAX_AGE_SEC:
                expected = self.expected_helper_sha256()
                installed = latest.get("helper_sha256") or {}
                for name, digest in sorted(expected.items()):
                    if installed.get(name) != digest:
                        problems.append(
                            f"hypervisor {name} installed {str(installed.get(name))[:12]} != repository {digest[:12]}"
                        )
        return "; ".join(problems) if problems else None

    def _deliver_files(self, name: str, facts: HostFacts) -> None:
        """The db VM's archive facts line goes where the archive-state readiness reads it, unchanged."""
        if self.archive_state_dir is None or not any(host.name == name and host.role == "db" for host in self.hosts):
            return
        text = facts.files.get("archive_state")
        if text is None:
            return
        try:
            json.loads(text)
        except ValueError:
            logger.warning("%s archive facts line is not JSON; last.json left as it was", name)
            return
        _atomic_write(self.archive_state_dir / "last.json", text if text.endswith("\n") else text + "\n")


def host_drift(host: VmHost, facts: Mapping[str, Any], excused: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """What differs from the install on one VM by its periodic read: a role unit not active, a checksum record that
    does not pass, a file changed since install. ``excused`` is decoys.held_excuses: a file a VM decoy held through
    the recording changed (D1 (b)) is no drift, on that node only."""
    problems: list[str] = []
    for unit, state in sorted(facts["units"].items()):
        if state != "active":
            problems.append(f"{host.name} {unit} {state}")
    held = set((excused or {}).get(host.name, {}).get("paths") or ())
    # war.sha256 names the WAR relative to the webapps directory; the CLI names files absolute.
    unexcused = [path for path in facts["drift"] if _drift_absolute(path) not in held]
    by_record = facts.get("drift_records") or {}  # facts read before L-D have none: nothing is excused
    for record in (*REQUIRED_RECORDS_BY_ROLE.get(host.role, ()), *REQUIRED_RECORDS_ALL):
        verdict = facts["sums"].get(record, "not read")
        # sha256sum -c ends 1 on any differing line; only a record whose every line is a held decoy's file
        # had its rc=1 from the decoy (review rv-t4u4 L-D: an rc=1 with no line of its own is not).
        lines = by_record.get(record) or []
        if verdict == "rc=1" and held and lines and all(_drift_absolute(path) in held for path in lines):
            continue
        if verdict != "rc=0":
            problems.append(f"{host.name} {record} {verdict}")
    for path in unexcused:
        problems.append(f"{host.name} changed since install: {path}")
    return problems


def server_pairs(first: VmDomain, second: VmDomain) -> list[tuple[str, str]]:
    """Twin servers of two VM testbeds: the n-th server of a role in one inventory with the n-th of that role in the
    other (the rule of testbed-services civil-petition/deploy/tools/pair_packages.py)."""
    def by_role(domain: VmDomain) -> dict[str, str]:
        counter: dict[str, int] = {}
        out: dict[str, str] = {}
        for host in domain.servers:
            index = counter.get(host.role, 0)
            counter[host.role] = index + 1
            out[f"{host.role}{index}"] = host.name
        return out

    one, two = by_role(first), by_role(second)
    return [(one[key], two[key]) for key in sorted(set(one) | set(two)) if key in one and key in two] + [
        (one.get(key, "-"), two.get(key, "-")) for key in sorted(set(one) ^ set(two))
    ]


def load_pair_expected(path: Path | None = None) -> dict[str, Any]:
    """The one expected value of pair condition 1 (PAIR_EXPECTED, made by testbed-services render.py; ST4 contract
    M2): ``pairs`` [{t, s, role, files {dest: normalized sha256}, t_only [dest], differs [dest]}] in the role order
    of pair_packages.py, and ``loadgen_files`` {"loadgen/<name>": sha256} for the load scripts. Raises with why when it cannot be
    read or has the wrong shape."""
    path = path or PAIR_EXPECTED
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") != PAIR_EXPECTED_SCHEMA:
            # Another shape may give the same fields another meaning (review rv-st4r L9): not read as this one.
            raise ValueError(f"schema {document.get('schema')!r}, expected {PAIR_EXPECTED_SCHEMA!r}")
        pairs = []
        for row in document["pairs"]:
            for field in ("t", "s", "role", "files", "t_only", "differs"):
                if field not in row:
                    raise ValueError(f"a pair lacks {field!r}")
            if not isinstance(row["t_only"], list) or not isinstance(row["differs"], list):
                raise ValueError("t_only and differs must be lists")
            pairs.append({
                "t": str(row["t"]), "s": str(row["s"]), "role": str(row["role"]),
                "files": {str(dest): str(value) for dest, value in dict(row["files"]).items()},
                "skip": {str(dest) for dest in [*row["t_only"], *row["differs"], *row.get("secrets", [])]},
            })
        loadgen = {str(name): str(value) for name, value in dict(document["loadgen_files"]).items()}
    except Exception as error:
        raise VmOpsError(f"pair expected values unreadable at {path}: {type(error).__name__}: {error}") from None
    if not pairs or not all(row["files"] for row in pairs) or not loadgen:
        raise VmOpsError(f"pair expected values at {path} have no pairs, an empty pair or no loadgen_files")
    return {"pairs": pairs, "loadgen_files": loadgen}


def _sum_lines(text: str | None) -> dict[str, str] | None:
    """`<sha256>  <path>` lines as {path: sha256}; None when the file is missing or has no line."""
    if not text:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        value, _, name = line.strip().partition("  ")
        if value and name:
            out[name.strip()] = value.strip()
    return out or None


LOADGEN_PREFIX = "loadgen/"


def pair_verdict(
    first: "VmOps", second: "VmOps", *, now: datetime | None = None,
    excused: Mapping[str, Mapping[str, Any]] | None = None, expected_path: Path | None = None,
) -> dict[str, Any]:
    """S design §2-4 condition 1 and §7-4 «the expected value is one, T's» as the runner can see it before a
    recording (ST4 contract M2). ``first`` is T, ``second`` S. On fresh periodic reads, for every pair of the
    repository's expected value (pair-expected.json): both VMs run what their own install left (host_drift empty;
    a held decoy's file excused as in the drift gate), and each VM's normalized checksums, without the pair's
    t_only and differs files, are exactly the pair's ``files``; the loadgens' load scripts are ``loadgen_files``;
    S's two WAS carry the WAR record line T's two WAS carry (one line). A missing expected file or record is «no
    pair». Not a gate: a run of either domain is recorded with ``pair`` true or false and why."""
    now = now or first.clock()
    differences: list[str] = []
    checked: list[str] = []
    try:
        expected = load_pair_expected(expected_path)
    except VmOpsError as error:
        return {"pair": False, "domains": [first.domain.key, second.domain.key], "checked": [],
                "differences": [str(error)], "checked_at": _format(now)}
    documents = {id(first): first.load(), id(second): second.load()}

    # Every line names its testbed: both loadgens are called «loadgen» (review rv-st4r L6).
    def facts_of(ops: "VmOps", name: str) -> tuple[VmHost | None, dict[str, Any] | None]:
        host = next((item for item in ops.hosts if item.name == name), None)
        if host is None:
            differences.append(f"{ops.domain.key}/{name}: not a VM of {ops.domain.key}")
            return None, None
        checked.append(f"{ops.domain.key}/{name}")
        facts = (documents[id(ops)]["hosts"].get(name) or {}).get("facts")
        if facts is None or (now - _parse(facts["read_at"])).total_seconds() > FACTS_MAX_AGE_SEC:
            differences.append(f"{ops.domain.key}/{name}: no fresh periodic read")
            return host, None
        differences.extend(f"{ops.domain.key}/{line}" for line in host_drift(host, facts, excused))
        return host, facts

    wars: dict[str, dict[str, str | None]] = {first.domain.key: {}, second.domain.key: {}}
    paired = set()
    wanted = len(server_pairs(first.domain, second.domain)) + 1  # the servers' twins and the two loadgens
    if len(expected["pairs"]) != wanted:
        differences.append(f"pair expected values have {len(expected['pairs'])} pairs, the testbeds have {wanted}")
    for row in expected["pairs"]:
        for ops, name in ((first, row["t"]), (second, row["s"])):
            paired.add((ops.domain.key, name))
            host, facts = facts_of(ops, name)
            if host is not None and host.role != row["role"]:
                differences.append(f"{ops.domain.key}/{name}: is a {host.role} VM, the expected pair says {row['role']}")
                continue
            if facts is None:
                continue
            name = f"{ops.domain.key}/{name}"
            files = facts.get("pair") or {}
            installed = _sum_lines(files.get("pair_normalized"))
            if installed is None:
                differences.append(f"{name}: no {NORMALIZED_RECORD}")
                continue
            scripts = {k: v for k, v in installed.items() if k.startswith(LOADGEN_PREFIX)}
            rendered = {k: v for k, v in installed.items() if not k.startswith(LOADGEN_PREFIX) and k not in row["skip"]}
            wrong = sorted(d for d in set(rendered) | set(row["files"]) if rendered.get(d) != row["files"].get(d))
            if wrong:
                differences.append(f"{name}: normalized checksums differ from the expected ones for "
                                   f"{', '.join(wrong[:5])}{' and more' if len(wrong) > 5 else ''}")
            if host.role == "loadgen" and scripts != expected["loadgen_files"]:
                differences.append(f"{name}: load scripts differ from the expected ones "
                                   f"({', '.join(sorted(set(scripts) | set(expected['loadgen_files'])))})")
            if host.role == "was":
                wars[ops.domain.key][host.name] = ((files.get("pair_war") or "").strip() or None)
    for ops in (first, second):
        for host in ops.hosts:
            if host.role in PAIR_ROLES and (ops.domain.key, host.name) not in paired:
                differences.append(f"{ops.domain.key}/{host.name}: no pair in the expected values")
    t_wars, s_wars = wars[first.domain.key], wars[second.domain.key]
    missing = sorted(f"{key}/{name}" for key, side in wars.items() for name, line in side.items() if line is None)
    if missing:
        differences.append(f"no WAR record on {', '.join(missing)}")
    t_lines = {line for line in t_wars.values() if line}
    if len(t_lines) != 1:
        differences.append(f"{first.domain.key} WAS carry {len(t_lines)} WAR record lines, expected one")
    elif any(line and line not in t_lines for line in s_wars.values()):
        differences.append(f"{second.domain.key} WAR record is not {first.domain.key}'s: "
                           + "; ".join(f"{n} {(v or '-')[:16]}" for n, v in sorted(s_wars.items())))
    return {"pair": not differences, "domains": [first.domain.key, second.domain.key], "checked": checked,
            "differences": differences, "checked_at": _format(now)}


# Routine operator sessions (testbed-services docs/spec-testbed-vm-3tier.md operator path, Q35 answer condition ②).
# Change-cause executors (router.config, vm.service, the U7 decoy WAR swap) log in as the operator (sysop) from the
# operator terminal address with a real pts session. If that login only happened at injections, «an operator
# login» would mark the cause time. So the runner opens the same session, the same way, read-only, all the time:
# one VM drawn evenly from the router and the four servers, every 30 to 90 minutes drawn evenly, recordings
# included. The session itself (ssh form, source address, role commands, the lock it shares with the executors)
# belongs to the testbed-services CLI; the runner only schedules it and records the outcome (T4O contract 3, 4, 6).
CONTRACT_ROOT = Path(os.environ.get("SCENARIO_CONTRACT_ROOT", "/opt/lucida/scenario-contracts"))
OPERATOR_SESSION_CLI = CONTRACT_ROOT / "profiles" / "operator_session.py"
# The VMs the CLI accepts come from the CLI itself (`--list-nodes`, one list in testbed-services, review rv-t4o L3);
# this is only what the runner expects to see there (the router and the four servers, never the loadgen).
OPERATOR_ROUTINE_HOSTS = ("rt-core01", "web01", "was01", "was02", "db01")
OPERATOR_ROUTINE_MIN_SEC = 1800
OPERATOR_ROUTINE_MAX_SEC = 5400
# The CLI exits 75 when an executor's operator session holds that VM's lock: same VM again after this.
OPERATOR_ROUTINE_BUSY_EXIT = 75
OPERATOR_ROUTINE_BUSY_RETRY_SEC = 300
# After this many busy answers in a row for the same VM, another VM is drawn (a lock held for good, e.g. by an ssh
# left behind, must not pin the routine to one VM; review rv-t4o M2).
OPERATOR_ROUTINE_BUSY_LIMIT = 3
# The CLI's own limit around its ssh session (T4S SESSION_TIMEOUT_SECONDS 90: it ends its ssh group and exits 124);
# the runner waits longer, so the CLI ends its own session first. 124 is recorded as a failure.
OPERATOR_SESSION_CLI_LIMIT_SEC = 90
# With no recorded session (first start) the first one comes 1 to 10 minutes after start.
OPERATOR_ROUTINE_FIRST_SEC = (60, 600)
OPERATOR_ROUTINE_TIMEOUT_SEC = OPERATOR_SESSION_CLI_LIMIT_SEC + 60
# Preflight: the last successful routine session (any VM) at most two longest gaps ago (T4O contract 6).
OPERATOR_ROUTINE_MAX_AGE_SEC = 2 * OPERATOR_ROUTINE_MAX_SEC
# What the CLI reads from the environment (same names as the executors).
OPERATOR_ENV_PASSTHROUGH = ("VM_OPS_KEY_DIR", "CIVIL_PETITION_SSH_CONTROL_DIR", "CIVIL_PETITION_DEPLOY_DIR")


def operator_environment() -> dict[str, str]:
    """What the operator CLI runs with: a plain PATH plus the key, socket and deploy directories it reads."""
    env = _environment()
    env.update({name: os.environ[name] for name in OPERATOR_ENV_PASSTHROUGH if os.environ.get(name)})
    return env


class OperatorRoutine:
    """Schedules routine operator sessions and keeps their record (state_dir/operator-routine.json)."""

    def __init__(
        self,
        *,
        process_runner: Callable[..., subprocess.CompletedProcess] = run_in_group,
        clock: Clock = _utc_now,
        state_dir: Path = STATE_DIR,
        rng: Any = None,
        cli: Path = OPERATOR_SESSION_CLI,
        cli_args: Sequence[str] = (),
    ) -> None:
        self.process_runner = process_runner
        # The domain's own node-list arguments (`--domain <key>` for a testbed other than T, ST4 contract 2: only
        # `--list-nodes` takes it; `--node` names are unique across the testbeds).
        self.cli_args = tuple(cli_args)
        self._nodes: tuple[str, ...] | None = None
        self.clock = clock
        self.state_dir = state_dir
        self.rng = rng or random.SystemRandom()
        self.cli = cli
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.state_dir / "operator-routine.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, document: Mapping[str, Any]) -> None:
        _atomic_write(self.path, json.dumps(document, indent=2, sort_keys=True) + "\n")

    def _call(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        return self.process_runner(
            ["python3", str(self.cli), *(self.cli_args if args[:1] == ["--list-nodes"] else ()), *args],
            timeout=OPERATOR_ROUTINE_TIMEOUT_SEC, env=self._environment(),
        )

    def nodes(self) -> tuple[str, ...]:
        """The VMs the CLI takes (`--list-nodes`), read once per process. Raises when it cannot be read."""
        if self._nodes is None:
            result = self._call(["--list-nodes"])
            if result.returncode != 0:
                raise VmOpsError(f"node list exited {result.returncode}: {_tail(result.stderr or result.stdout)}")
            names = json.loads(result.stdout)
            if not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names):
                raise VmOpsError(f"node list is not a list of names: {_tail(result.stdout)}")
            self._nodes = tuple(names)
        return self._nodes

    def _draw(self, now: datetime, *, first: bool = False, avoid: str | None = None) -> dict[str, str]:
        low, high = OPERATOR_ROUTINE_FIRST_SEC if first else (OPERATOR_ROUTINE_MIN_SEC, OPERATOR_ROUTINE_MAX_SEC)
        hosts = [name for name in self.nodes() if name != avoid] or list(self.nodes())
        return {"at": _format(now + timedelta(seconds=self.rng.uniform(low, high))), "host": self.rng.choice(hosts)}

    def _environment(self) -> dict[str, str]:
        return operator_environment()

    def tick(self) -> dict[str, Any]:
        """Run the session that is due, if any, and schedule the next. Returns the record."""
        with self._lock:
            now = self.clock()
            document = self.load()
            try:
                self.nodes()
            except Exception as error:
                # No list, no session: preflight stops the domain once the last success is 180 minutes old.
                document["stopped"] = {"at": _format(now), "detail": f"{type(error).__name__}: {error}"[:300]}
                self._save(document)
                return document
            document.pop("stopped", None)
            planned = document.get("next")
            if not planned or planned.get("host") not in self.nodes():
                document["next"] = self._draw(now, first=document.get("last_ok") is None)
                self._save(document)
                return document
            if now < _parse(planned["at"]):
                return document
            host = planned["host"]
            try:
                result = self._call(["--node", host, "--check"])
                code, detail = result.returncode, _tail(result.stderr or result.stdout)
            except subprocess.TimeoutExpired:
                code, detail = -1, f"no end within {OPERATOR_ROUTINE_TIMEOUT_SEC}s"
            except Exception as error:
                code, detail = -1, f"{type(error).__name__}: {error}"
            stamp = _format(now)
            document["last"] = {"at": stamp, "host": host, "rc": code, "detail": "" if code == 0 else detail[:300]}
            if code != OPERATOR_ROUTINE_BUSY_EXIT:
                document.pop("busy_host", None)
                document.pop("busy_count", None)
            if code == 0:
                document["last_ok"] = {"at": stamp, "host": host}
                document["next"] = self._draw(now)
            elif code == OPERATOR_ROUTINE_BUSY_EXIT:
                busy = int(document.get("busy_count", 0)) + 1 if document.get("busy_host") == host else 1
                if busy >= OPERATOR_ROUTINE_BUSY_LIMIT:
                    # Held too long: another VM on the normal schedule, and say so.
                    document["last"]["switched_from"] = host
                    document["last"]["detail"] = (
                        f"{host} busy {busy} times in a row; drew another VM. {document['last']['detail']}"[:300]
                    )
                    document["next"] = self._draw(now, avoid=host)
                    document.pop("busy_host", None)
                    document.pop("busy_count", None)
                else:
                    # An executor's operator session holds this VM: the same VM a little later.
                    document["next"] = {
                        "at": _format(now + timedelta(seconds=OPERATOR_ROUTINE_BUSY_RETRY_SEC)), "host": host,
                    }
                    document["busy_host"], document["busy_count"] = host, busy
            else:
                logger.warning("routine operator session on %s failed (%s): %s", host, code, detail)
                document["next"] = self._draw(now)
            self._save(document)
            return document

    def last_ok_age_sec(self, now: datetime) -> float:
        """Seconds since the last successful routine session, the limit + 1 when there was none."""
        last = self.load().get("last_ok")
        if not last:
            return float(OPERATOR_ROUTINE_MAX_AGE_SEC + 1)
        return max(0.0, (now - _parse(last["at"])).total_seconds())


class VmOpsService:
    """Background loop: master check every 30s, periodic read every 5 minutes, the routine operator session when it
    is due. Started from the app lifespan."""

    def __init__(
        self,
        ops: VmOps,
        *,
        check_interval_sec: float = MASTER_CHECK_INTERVAL_SEC,
        read_interval_sec: float = PERIODIC_READ_INTERVAL_SEC,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.ops = ops
        self.check_interval_sec = check_interval_sec
        self.read_interval_sec = read_interval_sec
        self._monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._routine_task: asyncio.Task | None = None

    async def run(self) -> None:
        next_read = self._monotonic()
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.ops.supervisor.ensure)
            except Exception:
                logger.exception("management ssh check failed")
            if self._monotonic() >= next_read:
                next_read = self._monotonic() + self.read_interval_sec
                try:
                    await asyncio.to_thread(self.ops.read_all)
                except Exception:
                    logger.exception("periodic management read failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval_sec)
            except asyncio.TimeoutError:
                pass

    async def run_routine(self) -> None:
        """The routine operator session in its own loop (review rv-t4o L4): a session takes up to the CLI limit and
        must not hold up the master checks."""
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.ops.routine.tick)
            except Exception:
                logger.exception("routine operator session failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval_sec)
            except asyncio.TimeoutError:
                pass

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self.run())
            self._routine_task = asyncio.create_task(self.run_routine())

    async def stop(self) -> None:
        """End the loop, then the masters with ``-O exit`` (review rv-t4r3 H1: a master killed by docker's SIGKILL
        leaves its socket file behind). The loop gets STOP_LOOP_WAIT_SEC; a check or read already running in its
        worker thread cannot be cancelled, so the supervisor waits for its lock (an open takes at most
        MASTER_OPEN_WAIT_SEC). The app stops this service first and compose gives the container stop_grace_period
        60s, longer than this path."""
        self._stop.set()
        for task in (self._task, self._routine_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=STOP_LOOP_WAIT_SEC)
            except Exception:
                task.cancel()
        await asyncio.to_thread(self.ops.supervisor.stop)


_ops: VmOps | None = None
_service: VmOpsService | None = None


def enabled() -> bool:
    return os.environ.get("CIVIL_PETITION_OPS", "").strip().lower() in {"1", "true", "yes", "on"}


def get_vm_ops() -> VmOps | None:
    """The process-wide VmOps when CIVIL_PETITION_OPS is on, else None (off by default)."""
    global _ops
    if not enabled():
        return None
    if _ops is None:
        archive = os.environ.get("PETITION_ARCHIVE_STATE_DIR", "").strip()
        _ops = VmOps(archive_state_dir=Path(archive) if archive else None)
    return _ops


def get_vm_ops_service() -> VmOpsService | None:
    global _service
    ops = get_vm_ops()
    if ops is None:
        return None
    if _service is None:
        _service = VmOpsService(ops)
    return _service


_sms_ops: VmOps | None = None
_sms_service: VmOpsService | None = None


def sms_enabled() -> bool:
    return os.environ.get("CIVIL_PETITION_SMS_OPS", "").strip().lower() in {"1", "true", "yes", "on"}


def get_sms_ops() -> VmOps | None:
    """The S testbed's VmOps when CIVIL_PETITION_SMS_OPS is on (off by default), its VMs read from SMS_INVENTORY.
    An unreadable inventory raises: the switch is on, so a silent «no S» would hide the domain's gate."""
    global _sms_ops
    if not sms_enabled():
        return None
    if _sms_ops is None:
        archive = os.environ.get("PETITION_SMS_ARCHIVE_STATE_DIR", "").strip()
        _sms_ops = VmOps(
            domain=load_inventory_domain(SMS_INVENTORY, key=SMS_DOMAIN),
            archive_state_dir=Path(archive) if archive else None,
        )
    return _sms_ops


def get_sms_ops_service() -> VmOpsService | None:
    global _sms_service
    ops = get_sms_ops()
    if ops is None:
        return None
    if _sms_service is None:
        _sms_service = VmOpsService(ops)
    return _sms_service


def ops_for(domain: str | None) -> VmOps | None:
    """The VmOps of a VM testbed domain, None when it is off or not a VM testbed."""
    if domain == DOMAIN:
        return get_vm_ops()
    if domain == SMS_DOMAIN:
        return get_sms_ops()
    return None


def current_pair_verdict(excused: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """pair_verdict of T and S as the queue records it with a run of either; «no pair» with why when one is off."""
    t_ops, s_ops = get_vm_ops(), get_sms_ops()
    if t_ops is None or s_ops is None:
        off = [key for key, ops in ((DOMAIN, t_ops), (SMS_DOMAIN, s_ops)) if ops is None]
        return {"pair": False, "domains": [DOMAIN, SMS_DOMAIN], "checked": [],
                "differences": [f"operations off for {', '.join(off)}: the twin cannot be read"],
                "checked_at": _format(_utc_now())}
    return pair_verdict(t_ops, s_ops, excused=excused)


def vm_domain_of(host_or_domain: str | None) -> VmDomain | None:
    """The VM testbed a domain key names (T fixed here, S from its inventory when its operations are on)."""
    if host_or_domain == DOMAIN:
        return T_DOMAIN
    if host_or_domain == SMS_DOMAIN:
        ops = get_sms_ops()
        return ops.domain if ops is not None else None
    return None
