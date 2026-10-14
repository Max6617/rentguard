"""rentguard command line.

    rentguard init                       # write a commented rentguard.toml
    rentguard doctor [--json]
    rentguard up --purpose "baseline" [--cap 5] [--dry-run]
    rentguard run -- python train.py
    rentguard status [--json]
    rentguard watch
    rentguard down [--json]
    rentguard ledger [--json]

Every command is safe to run when nothing is rented: the tool says so and exits non-zero rather
than inventing an instance. Every read command takes ``--json`` and prints exactly one JSON
document on stdout, which is what the MCP server hands back to a Bot as structured data.

Money-moving commands are ``up`` (rents), ``down`` (stops and closes the row) and ``adopt``
(records an instance that is already billing). Nothing else spends.

The rule this file encodes, in the two places it matters:

- **Before renting**, ``up`` checks three things: the recorded spend against the cap, whether a
  live instance exists that nothing has a row for, and whether an open row names an instance that
  is not there. All three are ways a second bill appears, and all three refuse.
- **The row comes before the machine.** ``up`` opens a provisional row before it calls ``create``,
  because a machine that bills with no record of it is the worst state this tool can leave behind —
  worse than a row that has to be corrected, and worse than a machine that is stopped. The row is
  finalised with the instance the provider names, and voided if nothing was ever created.
- **After the machine answers**, a row opened for an instance that was already billing (a resume, an
  ``adopt``) is backdated from the provider's own uptime, so hours that were never recorded are
  recorded at their real cost instead of starting the clock at zero.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import fnmatch
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import threading
from datetime import timedelta
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, api_key, load
from .guard import (
    CLAIM_ANOTHER_ROOT,
    CLAIM_THIS_ROOT,
    CLAIM_UNCLAIMED,
    Finding,
    heartbeat_age_minutes,
    snapshot,
    state_findings,
    watch as guard_watch,
)
from .ledger import JobLog, Ledger, iso, now_utc
from .provider import (
    Provider,
    ProviderError,
    available as providers_available,
    is_alive,
    make_provider,
    provider_name,
)
from .runpod import RunPod

DEFAULTS = {
    "image": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
    "cloud": "SECURE",
    "container_disk_gb": 50,
    "gpu_preference": ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"],
    "ports": ["22/tcp", "8888/http"],
    "name": "rentguard-workbench",
    "volume_id": "",
    "volume_mount": "/workspace",
}

LIVE_STATUSES = ("RUNNING", "PROVISIONING", "STARTING")

#: The tag on a row that was opened before its machine existed and has not been corrected yet. It is
#: what tells a row that merely *intended* a rental (void it) from one that recorded a real one.
PROVISIONAL_TAG = "provisional"

#: How much of a remote command's output a structured result carries, in characters. A training log
#: is unbounded and the tool result is not; what arrives is the tail, with the size it was cut from.
RUN_OUTPUT_LIMIT = 64 * 1024

# The JSON document a command prints is accumulated here, so a command body reads as ordinary
# printing code and the structured form is assembled in one place.
_REPORT: dict | None = None

#: What the last pull actually delivered. `down` pulls before it stops and holds only the exit code,
#: and it must say whether anything arrived and which patterns did not — a file count without the
#: unmatched patterns is the same false success one branch over. One entry, overwritten per pull.
_LAST_PULL: dict | None = None


def _pull_summary(**fields) -> None:
    global _LAST_PULL
    _LAST_PULL = fields


@contextlib.contextmanager
def json_report(args=None):
    """Collect a command's structured result and print it as exactly one JSON document.

    The document goes to stdout, and the prose a person reads goes through ``note()``, which sends
    it to stderr while a report is being collected. So ``--json`` still shows the text *and* leaves
    stdout parseable as one JSON value, and the MCP server can hand a Bot both.
    """
    global _REPORT
    if not json_wanted(args):
        yield
        return
    if _REPORT is not None:
        # Nested: a command called from inside another (the pull inside `down` or `run`) must add
        # to the caller's document, not print one of its own. Two documents on stdout is not JSON,
        # and the failure is silent — `json.loads` raises and the caller reports no structure at all.
        yield
        return
    previous = _REPORT
    _REPORT = {}
    try:
        yield
    finally:
        report, _REPORT = _REPORT, previous
        print(json.dumps(report or {}, indent=2, sort_keys=False))


def json_wanted(args=None) -> bool:
    if args is not None and getattr(args, "json", False):
        return True
    return _REPORT is not None


def emit(**fields) -> None:
    """Add fields to the JSON document, if one is being collected."""
    if _REPORT is not None:
        _REPORT.update(fields)


def note(text: str) -> None:
    """Human-readable output that must not corrupt a JSON document.

    With ``--json``, stdout carries exactly one JSON document and nothing else, so the prose a
    person would read goes to stderr. Without ``--json`` both go to stdout, as before. A machine
    that asked for JSON must be able to ``json.loads`` the whole of stdout.
    """
    if _REPORT is not None:
        print(text, file=sys.stderr)
    else:
        print(text)


# ------------------------------------------------------------------ plumbing

def _config() -> Config:
    return load()


def _provider(cfg: Config) -> Provider:
    """The provider for this config.

    ``RENTGUARD_PROVIDER=fake`` runs everything with no network and no money — the path the test
    suite uses, and a safe way for a new user to watch the tool work before renting anything.

    The fake is the one provider that keeps state between runs: without it, `up` would create a
    machine in one process that `status` and `run` in the next process could not see, and the
    documented offline flow would report drift against its own instance. Its state is a JSON file
    inside the state dir, next to the ledger, and only ever for the fake.
    """
    try:
        name = provider_name(cfg)
        kwargs = {"state_path": cfg.state_dir / "fake-state.json"} if name == "fake" else {}
        return make_provider(cfg, **kwargs)
    except ProviderError as exc:
        raise SystemExit(f"error: {exc}") from exc


def _read_state(cfg: Config) -> dict:
    path = cfg.state_path
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(cfg: Config, state: dict) -> None:
    path = cfg.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _pod(provider: Provider, cfg: Config) -> dict | None:
    """The recorded instance, if the provider still shows it.

    The recorded id is deliberately **not** cleared when the provider no longer returns it. That id
    is the only link between the state file and an open ledger row, and erasing it silently is how a
    run in progress becomes invisible — see F-1 in docs/KNOWN-FAILURES.md. Callers that mean to
    forget the instance do so explicitly.
    """
    instance_id = _read_state(cfg).get("instance_id")
    if not instance_id:
        return None
    pod = provider.get(instance_id)
    if pod is None or pod.get("status") == "TERMINATED":
        return None
    return pod


def _live(provider: Provider) -> list[dict]:
    """Every instance on the account that is billing, or as good as."""
    return [p for p in provider.list_pods() if p.get("status") in LIVE_STATUSES]


def _rp(cfg: Config, key: str, default=None):
    value = cfg.runpod.get(key, DEFAULTS.get(key, default))
    return default if value is None else value


def root_path() -> Path:
    """The project root this process is working in: ``$RENTGUARD_ROOT``, or the working directory."""
    return Path(os.environ.get("RENTGUARD_ROOT") or Path.cwd()).resolve()


def _root_tag(root: Path | str) -> str:
    """The eight hex characters that stand for one project root in an instance name.

    The tag is the only cross-root signal on the account: the ledger is a local file, so another
    root's rows are invisible here, and the pod list is not.
    """
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]


def _instance_name(cfg: Config) -> str:
    """The name the machine is rented under.

    When the config names nothing, the name is derived from the project root, so **two state dirs
    cannot share one account invisibly**: a pod rented from a different root carries a different
    name, and it is visible in the provider's console even when the ledger that would have recorded
    it is somewhere else. That is the whole reason this is not just ``"rentguard-workbench"`` —
    a fixed default is exactly how a rental gets orphaned from its books.
    """
    explicit = cfg.runpod.get("name")
    if explicit:
        return str(explicit)
    return f"rentguard-{_root_tag(root_path())}"


#: The shape of a root-tagged name: ``rentguard-<8 hex>``. Anything else is not a claim by a root —
#: which is why ``rentguard-workbench`` (an old fixed default, and the name of the stray A6000 that
#: burned $2.14) stays unclaimed rather than passing as somebody's project.
TAGGED_INSTANCE_NAME = re.compile(r"^rentguard-([0-9a-f]{8})$")


def instance_claim(cfg: Config, instance: dict) -> str:
    """Whose ledger an instance's **name** says it belongs to: one of the ``CLAIM_*`` values.

    This exists because "no row in this ledger" and "nobody recorded it" are different facts. A
    machine another root is paying for looks exactly like a stray from inside one root: the ledger is
    local, and the fix for a stray is to stop it. On 2026-10-14 that difference cost a false
    ``doctor`` error on a live pod whose row was open in another root's ledger — measured, pod
    ``2abx0hz2boe6ow``, row ``20261014T162601Z-4573d1`` in ``grok/pod-timing``.

    - a rentguard tag for a **different** root ⇒ another root rented it;
    - this project's own name (configured, or the tag this root computes) ⇒ possibly ours, possibly a
      sibling that copied the config — either way, absence of a row here proves nothing;
    - anything else ⇒ unclaimed, and only that is a hard error.

    The name is not proof, it is evidence. It is the strongest evidence available from one root, and
    it is used in the direction that cannot destroy anything: doubt keeps hands off a machine.
    """
    name = str((instance or {}).get("name") or "")
    if not name:
        return CLAIM_UNCLAIMED
    if name == _instance_name(cfg):
        return CLAIM_THIS_ROOT
    tagged = TAGGED_INSTANCE_NAME.match(name)
    if tagged is None:
        return CLAIM_UNCLAIMED
    return CLAIM_THIS_ROOT if tagged.group(1) == _root_tag(root_path()) else CLAIM_ANOTHER_ROOT


def _ssh(fmt: str) -> str:
    """Windows has no /dev/null; the known-hosts file has to point at NUL there."""
    return fmt.format(null="NUL" if os.name == "nt" else "/dev/null")


def _ssh_argv(cfg: Config, provider: Provider, pod: dict) -> tuple[list[str], str]:
    """The ssh command line for this instance.

    Access is a provider method, not a RunPod static: a second provider may reach its machines by a
    different route entirely, and the caller must not have to know which one it is holding.
    ``ssh_argv`` is part of the interface every provider implements, so it is called directly.

    There used to be a fallback here for "a provider that does not implement the interface", and it
    called ``RunPod.ssh_args`` — a method that no longer exists. It would have raised AttributeError
    on the one path it was written to protect, and it was unreachable in practice because the base
    class defines ``ssh_argv``. Dead and broken: deleted rather than covered.
    """
    key = Path(cfg.ssh_key) if cfg.ssh_key else None
    args, target = provider.ssh_argv(pod, key)
    args = [_ssh(a) for a in args]
    args = [cfg.ssh_binary, *args]
    # ssh wants the port before the target, which the provider has already arranged.
    return args, target


def _ssh_pty(provider: Provider, pod: dict) -> bool:
    """Whether this route's session is a PTY, so an archive cannot go down it unchanged.

    The provider owns the answer (it owns the route); this only asks it. A provider that predates
    the question — a foreign subclass, a test double — is treated as the plain byte channel, which
    is the behaviour every provider had before the proxy route was understood.
    """
    ask = getattr(provider, "ssh_requires_pty", None)
    return bool(ask(pod)) if callable(ask) else False


def _require_alive(provider: Provider, pod: dict) -> None:
    """Refuse to touch a machine that is not usable.

    ``RUNNING`` is a claim. A positive uptime and a port set are the evidence, and this is the
    single place that decides what the evidence means.
    """
    if not is_alive(pod):
        runtime = pod.get("runtime") or {}
        raise SystemExit(
            f"error: instance {pod.get('id')} reports {pod.get('status')} but is not usable "
            f"(uptime={runtime.get('uptime')}, ports="
            f"{'set' if runtime.get('ports') else 'NULL'}).\n"
            "A pod that stays RUNNING with uptime 0 and null ports is crash-looping. "
            "Not uploading to it.")


def _findings_dicts(findings: list[Finding]) -> list[dict]:
    return [{"code": f.code, "severity": f.severity, "message": f.message} for f in findings]


def _open_run_dict(ledger: Ledger) -> dict | None:
    open_run = ledger.open_run()
    if open_run is None:
        return None
    return {
        "run_id": open_run.get("run_id"),
        "purpose": open_run.get("purpose"),
        "instance_id": open_run.get("instance_id"),
        "started_at": open_run.get("started_at"),
        "rate_usd_per_hour": float(open_run.get("rate_usd_per_hour") or 0.0),
        "accrued_usd": ledger.live_usd(),
    }


def _run_dict(run: dict | None) -> dict | None:
    if not run:
        return None
    return {"run_id": run.get("run_id"), "purpose": run.get("purpose"),
            "instance_id": run.get("instance_id"),
            "rate_usd_per_hour": run.get("rate_usd_per_hour"),
            "started_at": run.get("started_at")}


def _closed_dict(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"run_id": row.get("run_id"), "purpose": row.get("purpose"),
            "wall_clock_hours": row.get("wall_clock_hours"),
            "estimated_usd": row.get("estimated_usd"), "closed_by": row.get("closed_by"),
            "stopped_at": row.get("stopped_at")}


# ------------------------------------------------------------------ init

_TEMPLATE_HEAD = """\
# rentguard configuration.
#
# Every value below is what rentguard already uses, written out so nothing starts from an empty
# file. Delete anything you do not want to override; the defaults live in the code, not in this
# comment, so this file can never drift from them.
#
# Money rule: the cap is enforced outside the provider, in rentguard itself. Setting it here is the
# whole promise — a machine that reaches the cap is stopped by something other than your memory.
# Run `rentguard doctor` before you rent anything.
"""


def _toml_str(value) -> str:
    """A value rendered as a TOML basic string.

    Windows paths are why this exists: ``"C:\\Users\\me"`` inside a basic string is read as a ``\\U``
    escape and TOML rejects the whole file with "Invalid hex value". A generated config that cannot
    be parsed is worse than no config at all, so every value that comes from a variable — and every
    path in particular — goes through here.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _template(cfg: Config) -> str:
    b, a, rp = cfg.budget, cfg.artefacts, cfg.runpod
    lines = [_TEMPLATE_HEAD, ""]
    lines += [
        "# Which cloud. Registered providers: " + ", ".join(providers_available()) + ".",
        '# "fake" is an in-memory provider: it rents nothing, costs nothing, and is how you can try',
        "# every command before an account exists.",
        f'provider = {_toml_str(cfg.provider)}',
        "",
        "# Where the ledger, the state file and the guard heartbeat live, relative to the project root.",
        'state_dir = ".rentguard"',
        "",
        "# Power-user options, uncomment when needed:",
        '# ssh_binary = "ssh"',
        '# ssh_key = ""                     # empty means: use the provider\'s own key material',
        "",
        "# Keys above a [table] header are top-level; keys below one belong to that table. Keep every",
        "# top-level key above the first header, or it silently becomes part of a table and is ignored.",
        "",
        "[budget]",
        f"cap_usd = {b.cap_usd}          # hard ceiling for the whole project, across all runs",
        f"per_run_cap_usd = {b.per_run_cap_usd}     # ceiling for a single open run",
        f"max_hours_per_run = {b.max_hours_per_run}   # wall-clock ceiling for one rental",
        f"idle_minutes = {b.idle_minutes:g}        # stop after this long with the GPU idle; 0 disables",
        f"stop_timeout_s = {b.stop_timeout_s:g}       # a stop call slower than this escalates to the",
        "#                                provider's own stop endpoint: a hung socket must never hold",
        "#                                a billing machine open",
        "# balance_usd would be an OPTIONAL local assumption, never a reading. No provider exposes a",
        "# reliable balance, so rentguard never reports one: it reports spend against your cap.",
        "",
        "[artefacts]",
        "# Pulled when each `run` finishes and again on `down`, before the machine stops. An",
        "# instance with no network volume is a container: restart it and these files are gone.",
        'pull = ["results/**/*.json", "logs/*.log"]',
        f"local_dir = {_toml_str(a.local_dir)}",
        f"remote_dir = {_toml_str(a.remote_dir)}",
        f"pull_timeout_s = {a.pull_timeout_s:g}    # one ssh pull may take this long, then it is killed",
        "#                                and reported; the pull runs before a stop, so it is bounded",
        "",
        "[runpod]",
        f'cloud = {_toml_str(rp.get("cloud", DEFAULTS["cloud"]))}              # SECURE or COMMUNITY',
        f'image = {_toml_str(rp.get("image", DEFAULTS["image"]))}',
        f'container_disk_gb = {rp.get("container_disk_gb", DEFAULTS["container_disk_gb"])}',
        "# First GPU with stock wins. Prices move; check before you rent.",
        f'gpu_preference = {json.dumps(rp.get("gpu_preference", DEFAULTS["gpu_preference"]))}',
        f'volume_id = {_toml_str(rp.get("volume_id", ""))}       # empty means no network volume',
        f'volume_mount = {_toml_str(rp.get("volume_mount", DEFAULTS["volume_mount"]))}   # mounted here',
    ]
    return "\n".join(lines) + "\n"


def _config_file_default() -> Path:
    root = Path(os.environ.get("RENTGUARD_ROOT") or Path.cwd())
    return root / "rentguard.toml"


def cmd_init(args) -> int:
    cfg = _config()
    target = Path(args.path) if args.path else _config_file_default()
    text = _template(cfg)
    with json_report(args):
        emit(command="init", path=str(target))
        if args.print:
            note(text)
            emit(written=False, content=text)
            return 0
        if target.exists() and not args.force:
            note(f"refusing to overwrite {target} (pass --force)")
            emit(written=False, reason="exists")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        emit(written=True)
        note(f"wrote {target}")
        note("next           : `rentguard doctor`, then `rentguard up --purpose ...`")
    return 0


# ------------------------------------------------------------------ read commands

def cmd_doctor(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    name = provider_name(cfg)
    open_run = ledger.open_run()
    age = heartbeat_age_minutes(cfg.heartbeat_path)
    findings: list[Finding] = []
    with json_report(args):
        emit(command="doctor", version=__version__,
             config=str(cfg.source) if cfg.source else None,
             state_dir=str(cfg.state_dir), provider=name,
             api_key="present" if api_key() else ("not needed" if name == "fake" else "missing"))
        note(f"rentguard      : v{__version__}")
        note(f"config         : {cfg.source or '(none — using defaults)'}")
        note(f"state dir      : {cfg.state_dir}")
        note(f"provider       : {name}"
              + ("  (in-memory: no network, no spend)" if name == "fake" else ""))
        note(f"api key        : {'present' if api_key() else 'MISSING ($DSH_SECRET_RUNPOD_API_KEY)'}"
              + ("  (not needed for the fake)" if name == "fake" else ""))
        # One figure for the money, with its parts named. The closed rows are *recorded*; the open
        # run is *accruing* — and the cap is enforced against their sum (`up` checks
        # `total_usd() + live_usd()`, and the guard's snapshot prices the open run too). Printing
        # the closed total alone showed a human $0.61 while the tool enforced $1.42: the reading and
        # the enforcement have to be the same number (2026-10-14, a live $0.89/h window).
        closed_usd = ledger.total_usd()
        accruing_usd = ledger.live_usd()
        note(f"spend recorded : ${closed_usd:.2f} closed + ${accruing_usd:.2f} accruing = "
             f"${closed_usd + accruing_usd:.2f} of a ${cfg.budget.cap_usd:.2f} cap "
             f"({len(ledger.entries())} run(s), the open one included as an estimate from its start)")
        emit(spend={"recorded_usd": closed_usd, "accruing_usd": accruing_usd,
                    "total_usd": round(closed_usd + accruing_usd, 6),
                    "cap_usd": cfg.budget.cap_usd,
                    "runs": len(ledger.entries()),
                    "open_run_accrued_usd": accruing_usd})
        note(f"open run       : {open_run['run_id'] if open_run else '(none)'}")
        emit(open_run=_open_run_dict(ledger))

        # A ledger line that cannot be parsed is spend that has gone quiet. It is reported, not
        # dropped: an interrupted append is exactly the moment money stops being visible.
        malformed = ledger.parse_errors()
        if malformed:
            detail = "; ".join(f"line {m['line']}: {m['text'][:60]}" for m in malformed[:3])
            findings.append(Finding(
                code="ledger-line-unreadable", severity="error",
                message=f"{len(malformed)} unreadable ledger line(s) — an interrupted write may "
                        f"have lost a run ({detail})"))
            note(f"ledger lines   : {len(malformed)} UNREADABLE (corrupt or truncated) — "
                  f"see the findings below")
        else:
            note("ledger lines   : all readable")
        emit(malformed_ledger_lines=malformed)

        emit(guard={"heartbeat_age_minutes": age, "stale": age is None or age > 5})
        note(f"guard heartbeat: "
              f"{'(never)' if age is None else f'{age:.1f} min ago'}"
              f"{'  <-- STALE, nothing is watching' if (age is None or age > 5) else ''}")

        if name != "fake" and not api_key():
            findings.append(Finding(code="no-api-key", severity="error",
                                    message="no API key: the account cannot be inspected"))
            note("")
            for finding in findings:
                note(f"  {finding.severity.upper():<5} {finding.code}: {finding.message}")
            emit(findings=_findings_dicts(findings), live_instances=[])
            return 1

        try:
            provider = _provider(cfg)
        except SystemExit as exc:
            findings.append(Finding(code="provider-unavailable", severity="error",
                                    message=str(exc)))
            emit(findings=_findings_dicts(findings), live_instances=[])
            # Print the finding, not just the raw error: a script and a person both look for the
            # code, and the branch used to record it and return without ever showing it.
            note("")
            for finding in findings:
                note(f"  {finding.severity.upper():<5} {finding.code}: {finding.message}")
            return 1

        state = _read_state(cfg)
        tracked = state.get("instance_id")

        def probe(instance_id: str) -> dict | None:
            """What the provider says about an instance: its payload, ``{}`` for gone, ``None`` for
            "could not ask". The distinction matters: an unaskable provider is not proof of drift."""
            try:
                return provider.get(instance_id) or {}
            except ProviderError:
                return None

        findings.extend(state_findings(state, open_run, provider_gate=probe,
                                       claim=lambda pod: instance_claim(cfg, pod)))
        try:
            pods = provider.list_pods()
            pods_error = None
        except ProviderError as exc:
            pods = []
            pods_error = str(exc)
            note(f"provider error : {exc}")
        live = [p for p in pods if p.get("status") in LIVE_STATUSES]
        note(f"account pods   : {len(pods)} total, {len(live)} live")
        emit(live_instances=[{"id": p.get("id"), "status": p.get("status"),
                              "name": p.get("name"), "claim": instance_claim(cfg, p),
                              "cost_usd_per_hour": p.get("cost"),
                              "gpu": (p.get("gpu") or {}).get("id"),
                              "tracked": p.get("id") == tracked} for p in live])
        for pod in live:
            known = pod.get("id") == tracked
            note(f"  {pod.get('id')}  {str(pod.get('status')):<12} ${pod.get('cost')}/h  "
                  f"{str((pod.get('gpu') or {}).get('id')):<28} "
                  f"{'tracked' if known else 'UNTRACKED'}"
                  + (f"  name={pod.get('name')!r}" if pod.get("name") else ""))
        if not live:
            note("  (nothing is billing)")

        # Storage bills whether or not a pod exists, so a report that lists only pods can be right
        # about every instance and still miss the largest recurring cost on the account. Severity is
        # "warn": keeping a volume is legitimate, but paying for one that nothing uses is not
        # something anyone should have to remember. Doctor's hard errors stay reserved for a machine
        # that is billing with no record of it.
        try:
            volumes = provider.volumes()
            volumes_error = None
        except ProviderError as exc:
            volumes = []
            volumes_error = str(exc)
        def references(pod: dict, volume_id: str | None) -> bool:
            """A pod uses a volume if it names it at the top level or in a network mount."""
            if volume_id is None:
                return False
            if pod.get("volumeId") == volume_id:
                return True
            for mount in ((pod.get("mounts") or {}).get("network") or []):
                if mount.get("volumeId") == volume_id:
                    return True
            return False

        live_pods = [p for p in pods if p.get("status") in LIVE_STATUSES]

        def holders(volume: dict) -> tuple[str | None, str | None]:
            """(the live pod using it, a stopped pod still configured with it)."""
            volume_id = volume.get("id")
            if volume.get("attached_to"):
                return str(volume["attached_to"]), None
            live = next((p.get("id") for p in live_pods if references(p, volume_id)), None)
            stopped = next((p.get("id") for p in pods
                            if p.get("status") not in LIVE_STATUSES and references(p, volume_id)),
                           None)
            return live, stopped

        def attached_to(volume: dict) -> str | None:
            return holders(volume)[0]

        storage_monthly = round(sum(float(v.get("monthly_usd") or 0.0) for v in volumes), 2)
        note(f"network volumes: {len(volumes)}  (${storage_monthly:.2f}/month, billed while nothing "
             f"runs)")
        for volume in volumes:
            live_holder, stopped_holder = holders(volume)
            if live_holder:
                state_text = f"attached to {live_holder}"
            elif stopped_holder:
                state_text = (f"in the config of STOPPED pod {stopped_holder} — nothing is using it "
                              f"and it bills anyway")
            else:
                state_text = "attached to NOTHING — it bills with nothing using it"
            note(f"  {volume.get('name')}  {float(volume.get('size_gb') or 0):.0f} GB  "
                 f"{volume.get('region') or '?'}  ${float(volume.get('monthly_usd') or 0):.2f}/month"
                 f"  {state_text}")
            if live_holder is None:
                findings.append(Finding(
                    code="storage-unattached", severity="warn",
                    message=f"volume {volume.get('name')!r} "
                            f"({float(volume.get('size_gb') or 0):.0f} GB, "
                            f"{volume.get('region')}) costs "
                            f"${float(volume.get('monthly_usd') or 0):.2f}/month and no running "
                            f"instance uses it; delete it if it is not wanted"))
        if volumes:
            findings.append(Finding(
                code="storage-not-in-ledger", severity="warn",
                message=f"${storage_monthly:.2f}/month of network storage is NOT in the ledger "
                        f"total, which covers rentals only"))
        if volumes_error:
            findings.append(Finding(code="provider-error", severity="warn",
                                    message=f"could not list network volumes: {volumes_error}"))

        emit(network_volumes=[{**v, "attached_to": attached_to(v)} for v in volumes],
             storage_monthly_usd=storage_monthly)

        # A live instance with no row *here* is not automatically a stray: the ledger is a local file,
        # so another root's row for this machine is invisible, and the fix for a stray is to stop it.
        # The name decides what can be said out loud. Only an unclaimed name is a hard error.
        untracked = [p for p in live if p.get("id") != tracked]
        unclaimed = [p for p in untracked if instance_claim(cfg, p) == CLAIM_UNCLAIMED]
        foreign = [p for p in untracked if instance_claim(cfg, p) == CLAIM_ANOTHER_ROOT]
        ours = [p for p in untracked if instance_claim(cfg, p) == CLAIM_THIS_ROOT]
        if unclaimed:
            findings.append(Finding(
                code="live-instance-no-ledger-row", severity="error",
                message=f"{len(unclaimed)} live instance(s) with no ledger row and no project's name: "
                        f"{', '.join(str(p.get('id')) for p in unclaimed)}. Either `rentguard "
                        f"adopt <id> --purpose ...` or stop them."))
        if foreign:
            names = ", ".join(f"{p.get('id')} ({p.get('name')!r})" for p in foreign)
            findings.append(Finding(
                code="tracked-by-another-root", severity="warn",
                message=f"{len(foreign)} live instance(s) belong to another rentguard root by name: "
                        f"{names}. That root's ledger owns them; do not stop them from here, and do "
                        f"not `adopt` them here — either would double-count a machine someone else "
                        f"is paying for."))
        if ours:
            findings.append(Finding(
                code="row-not-in-this-root", severity="warn",
                message=f"{len(ours)} live instance(s) carry this project's name but have no row "
                        f"here ({', '.join(str(p.get('id')) for p in ours)}): the row may be in "
                        f"another root's ledger, or the state file that named them was lost. Check "
                        f"before stopping them."))
        if pods_error:
            findings.append(Finding(code="provider-error", severity="warn",
                                    message=f"could not list account pods: {pods_error}"))

        emit(findings=_findings_dicts(findings))
        if findings:
            note("")
            for finding in findings:
                note(f"  {finding.severity.upper():<5} {finding.code}: {finding.message}")
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_status(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)
    open_run = ledger.open_run()
    pod = _pod(provider, cfg)
    age = heartbeat_age_minutes(cfg.heartbeat_path)
    with json_report(args):
        emit(command="status", provider=provider_name(cfg),
             recorded_instance_id=_read_state(cfg).get("instance_id"),
             guard={"heartbeat_age_minutes": age, "stale": age is None or age > 5})

        if pod is None and open_run is not None:
            # The state in which money hides: the ledger believes a machine is rented and the
            # provider does not show it. This is the headline, not a footnote.
            accrued = ledger.live_usd()
            note(f"DRIFT          : run {open_run.get('run_id')} is open, but the provider shows no "
                  f"instance {open_run.get('instance_id')!r}")
            note(f"accrued        : ${accrued:.3f} at "
                  f"${float(open_run.get('rate_usd_per_hour') or 0):.2f}/h since "
                  f"{open_run.get('started_at')}")
            note(f"this run       : ${accrued:.3f} of ${cfg.budget.per_run_cap_usd:.2f}")
            note(f"recorded spend : ${ledger.total_usd():.2f} of ${cfg.budget.cap_usd:.2f} "
                  f"(an open row is not counted until it is closed, so this figure reads low)")
            note(f"purpose        : {open_run.get('purpose')}")
            note("next           : `rentguard down` closes the row and records the cost; "
                  "`rentguard adopt <id>` if the machine does still exist")
            emit(instance=None, drift=True, open_run=_open_run_dict(ledger),
                 spent={"recorded_usd": ledger.total_usd(),
                        "accrued_usd": accrued,
                        "total_usd": round(ledger.total_usd() + accrued, 6),
                        "cap_usd": cfg.budget.cap_usd})
            return 0

        if pod is None:
            note("no instance recorded")
            note(f"spend so far   : ${ledger.total_usd():.2f} of ${cfg.budget.cap_usd:.2f}")
            emit(instance=None, drift=False, open_run=None,
                 spent={"recorded_usd": ledger.total_usd(), "accrued_usd": 0.0,
                        "total_usd": ledger.total_usd(), "cap_usd": cfg.budget.cap_usd})
            return 0

        snap = snapshot(pod, cfg, ledger)
        stopped = str(snap.status).upper() in ("EXITED", "TERMINATED")
        note(f"instance       : {pod.get('id')}  {snap.status}")
        note(f"gpu            : {(pod.get('gpu') or {}).get('id')}  ${snap.rate_usd_per_hour}/h")
        note(f"uptime         : {snap.uptime_hours:.2f} h   gpu util {snap.gpu_util}%")
        if stopped:
            # The normal end of a rental. "Not usable" would be true but misleading: a stopped
            # machine is finished, not broken, and saying "crash loop" here would make the one
            # warning that matters unreadable.
            note(f"machine        : stopped ({snap.status}) — nothing is billing for it now")
        elif not is_alive(pod):
            runtime = pod.get("runtime") or {}
            note(f"usable         : NO — RUNNING is a claim, and this one has "
                  f"uptime={runtime.get('uptime')} and "
                  f"ports={'set' if runtime.get('ports') else 'NULL'}: a crash loop, not a "
                  f"machine to upload to")
        if not stopped and not snap.recorded:
            # "Nothing recording it" is a claim about the account, and a ledger is local: another
            # root's row for this machine is invisible here. Say what is actually known.
            if instance_claim(cfg, pod) == CLAIM_UNCLAIMED:
                note(f"ledger         : NO ROW for this instance — ${snap.run_usd:.3f} has been spent "
                     f"with nothing recording it (`rentguard adopt {pod.get('id')}`)")
            else:
                note(f"ledger         : no row in THIS root for this instance; its name "
                     f"{pod.get('name')!r} is a rentguard project name, so the row may be in another "
                     f"root's ledger — check there before adopting (adopting here would record the "
                     f"same machine twice)")
        note(f"this run       : ${snap.run_usd:.3f} of ${snap.per_run_cap_usd:.2f}")
        line = f"all runs       : ${snap.spent_usd:.3f} of ${snap.cap_usd:.2f}"
        # Time-to-cap is only meaningful for a machine that is actually billing.
        if snap.status == "RUNNING" and snap.minutes_to_cap:
            line += f"  (~{snap.minutes_to_cap} min to the cap at this rate)"
        note(line)
        if age is None:
            note("guard          : NO GUARD — nothing is watching this instance, so it will bill "
                 "until someone stops it")
        elif age > 5:
            note(f"guard          : probably dead (heartbeat {age:.1f} min old) — nothing is "
                 f"watching this instance, so it will bill until someone stops it")
        else:
            note(f"guard          : alive (heartbeat {age:.1f} min ago)")
        emit(instance={"id": pod.get("id"), "status": snap.status,
                       "gpu": (pod.get("gpu") or {}).get("id"),
                       "uptime_hours": snap.uptime_hours,
                       "gpu_util": snap.gpu_util,
                       "usable": is_alive(pod),
                       "rate_usd_per_hour": snap.rate_usd_per_hour},
             open_run=_open_run_dict(ledger), drift=False,
             spent={"recorded_usd": ledger.total_usd(),
                    "accrued_usd": snap.run_usd,
                    "total_usd": snap.spent_usd,
                    "cap_usd": snap.cap_usd,
                    "per_run_cap_usd": snap.per_run_cap_usd,
                    "remaining_usd": snap.remaining_usd,
                    "minutes_to_cap": snap.minutes_to_cap if snap.status == "RUNNING" else None})
    return 0


def cmd_ledger(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    rows = ledger.entries()
    open_run = ledger.open_run()
    with json_report(args):
        emit(command="ledger",
             runs=rows,
             total_usd=ledger.total_usd(),
             accrued_usd=ledger.live_usd(),
             total_with_open_usd=round(ledger.total_usd() + ledger.live_usd(), 6),
             cap_usd=cfg.budget.cap_usd,
             remaining_usd=round(cfg.budget.cap_usd - ledger.total_usd(), 6),
             open_run=_open_run_dict(ledger),
             malformed_lines=ledger.parse_errors())
        if not rows:
            note("no runs recorded")
        else:
            note(f"{'run':<22} {'gpu':<26} {'hours':>7} {'usd':>8}  {'closed by':<18} purpose")
            note("-" * 118)
            for row in rows:
                hours = row.get("wall_clock_hours")
                usd = row.get("estimated_usd")
                note(f"{str(row.get('run_id'))[:22]:<22} {str(row.get('gpu_id'))[:26]:<26} "
                      f"{(f'{hours:.2f}' if hours is not None else 'OPEN'):>7} "
                      f"{(f'{usd:.4f}' if usd is not None else '-'):>8}  "
                      f"{str(row.get('closed_by') or '-'):<18} {row.get('purpose')}")
            note("-" * 118)
            note(f"{'total':<22} {'':<26} "
                  f"{sum(float(r.get('wall_clock_hours') or 0) for r in rows):>7.2f} "
                  f"{ledger.total_usd():>8.4f}")
            if open_run:
                note(f"open row       : {open_run.get('run_id')}  accrued "
                      f"${ledger.live_usd():.4f} (not in the total until closed)")
            note(f"cap: ${cfg.budget.cap_usd:.2f}   remaining: "
                  f"${cfg.budget.cap_usd - ledger.total_usd():.2f}")
        # Always shown, including when nothing has ever been rented — which is exactly when a
        # standing storage cost is easiest to miss. A volume bills every month whether or not a pod
        # exists, so this line is the difference between a total and a fraction of the truth.
        note("storage: NOT included above — network volumes bill monthly while nothing runs; "
             "`rentguard doctor` lists them with their cost")
        emit(storage_excluded_from_total=True)
    return 0


# ------------------------------------------------------------------ money commands

def cmd_up(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)

    if args.cap is not None:
        cfg.budget.cap_usd = args.cap
    if args.per_run_cap is not None:
        cfg.budget.per_run_cap_usd = args.per_run_cap

    with json_report(args):
        already = round(ledger.total_usd() + ledger.live_usd(), 6)
        if already >= cfg.budget.cap_usd and not args.force:
            note(f"error: ${already:.2f} already recorded (including the open run's accrued cost) "
                  f"against a ${cfg.budget.cap_usd:.2f} cap. "
                  f"Raise the cap deliberately (--cap N) or pass --force. This is a money decision.")
            emit(action="refused", reason="cap-reached", spent_usd=already,
                 cap_usd=cfg.budget.cap_usd)
            return 2

        # An open row whose instance has vanished is still money, and it is already counted: the
        # check above adds `ledger.live_usd()` to the total, so `cap-reached` fires for a vanished
        # instance exactly as it does for a visible one. There used to be a second check here for
        # the vanished case; it could never be reached, because `already >= cap` implies it. Dead
        # code, deleted rather than covered.
        open_run = ledger.open_run()

        pod = _pod(provider, cfg)

        if pod is not None:
            state = _read_state(cfg)
            mismatches = _config_mismatch(cfg, pod, state)
            status = pod.get("status")
            starting = status in ("EXITED", "ERROR")
            forced = bool(getattr(args, "force", False))
            if mismatches:
                if starting and not forced:
                    note("error: the recorded instance is not what this config asks for, so it will "
                         "not be started:")
                    for line in mismatches:
                        note(f"  {line}")
                    note("  terminate it (`rentguard down`) and rent the config's card, or pass "
                         "--force to start it anyway — the override is recorded")
                    emit(action="refused", reason="recorded-instance-off-config",
                         instance={"id": pod.get("id"), "status": status},
                         mismatches=mismatches)
                    return 2
                overridden = bool(forced and starting)
                note(f"warning: instance {pod['id']} does not match this config"
                     + (" and --force overrides that" if overridden else "") + ":")
                for line in mismatches:
                    note(f"  {line}")
                # Recorded, not only printed: the failure this defect is about was a silent action,
                # and a warning on stdout is invisible to whoever reads the state afterwards.
                state["config_override"] = {"at": iso(now_utc()), "instance_id": pod.get("id"),
                                            "forced": overridden, "mismatches": mismatches}
                _write_state(cfg, state)
                emit(config_override={"forced": overridden, "mismatches": mismatches})
            if status in ("EXITED", "ERROR"):
                if getattr(args, "dry_run", False):
                    note(f"dry run: would resume instance {pod['id']} (was {status})")
                    emit(action="dry-run", instance_id=pod.get("id"), resumed=True)
                    return 0
                note(f"resuming instance {pod['id']} (was {status})")
                provider.action(pod["id"], "start")
                pod = provider.wait_running(pod["id"])
            else:
                note(f"instance {pod['id']} is already {status}")
            run = _open_if_needed(cfg, ledger, pod, args.purpose, provider)
            _print_access(cfg, provider, pod)
            emit(action="reused", instance={"id": pod.get("id"), "status": pod.get("status"),
                                            "cost_usd_per_hour": pod.get("cost")},
                 run=_run_dict(run), cap={"cap_usd": cfg.budget.cap_usd,
                                          "per_run_cap_usd": cfg.budget.per_run_cap_usd})
            return 0

        # A live instance that nothing recorded. `state.json` may be empty or truncated, and a
        # machine that is billing must not be forgotten just because the file is unreadable. The
        # ledger is the record that counts: an instance the ledger names is accounted for even when
        # the state file lost it.
        recorded = {r.get("instance_id") for r in ledger.entries() if r.get("instance_id")}
        if open_run:
            recorded.add(open_run.get("instance_id"))
        unknown = [p for p in _live(provider) if p.get("id") not in recorded]
        if unknown and not args.force:
            ids = ", ".join(str(p.get("id")) for p in unknown)
            note(f"error: {len(unknown)} live instance(s) with no ledger row: {ids}. Renting "
                  f"again would start a second bill. Adopt it "
                  f"(`rentguard adopt <id> --purpose ...`) or stop it first.")
            emit(action="refused", reason="live-untracked-instance",
                 untracked=[p.get("id") for p in unknown])
            return 2

        def request_kwargs(gpu_id: str) -> dict:
            return dict(
                gpu_id=gpu_id,
                image=_rp(cfg, "image"),
                container_disk_gb=int(_rp(cfg, "container_disk_gb")),
                cloud=_rp(cfg, "cloud"),
                ports=list(_rp(cfg, "ports")),
                name=_instance_name(cfg),
                volume_id=(_rp(cfg, "volume_id") or None),
                volume_mount=_rp(cfg, "volume_mount"),
            )

        # Availability in the catalogue is not capacity. The M2 rental found the RTX 3070 and the
        # A4000 both offered and both refused at create ("no longer any instances available"), so the
        # preference list is a list to walk, not a single guess: try each offered GPU and keep the
        # first the provider actually accepts. Every failed attempt is printed, because that is the
        # difference between "the first choice was out of stock" and "the tool picked something
        # else without saying so".
        errors: list[str] = []
        try:
            offered = [args.gpu] if args.gpu else provider.candidates(
                _rp(cfg, "gpu_preference"), cloud=_rp(cfg, "cloud"))
        except ProviderError as exc:
            note(f"error: {exc}")
            emit(action="refused", reason="no-gpu-available", detail=str(exc))
            return 1

        # The dry-run check comes *before* the create loop, not after it. A `--dry-run` that walks
        # the preference list to learn which GPU would be accepted is spending money to answer a
        # question about not spending money: on the real account it made three create calls and then
        # printed "nothing was rented and nothing was spent". Say what would be tried, touch nothing.
        if getattr(args, "dry_run", False):
            first = offered[0]
            fallbacks = f" (then {', '.join(offered[1:])})" if len(offered) > 1 else ""
            note(f"dry run: would request {first}{fallbacks}; nothing was rented and nothing "
                  f"was spent")
            emit(action="dry-run", gpu_id=first, candidates=list(offered), instance_id=None)
            return 0

        # The row opens *before* the create loop. `create` is the irreversible step and the moment
        # money starts, so everything after this line has a row behind it, whatever happens next —
        # including a kill the process cannot catch. Defect 10 was this window: the row used to open
        # only after the second alive reading, so an interruption during `wait_running` (minutes on a
        # real pod) left a machine billing that no ledger named. See `_open_provisional`.
        provisional = _open_provisional(cfg, ledger, provider, offered[0], args.purpose,
                                        cloud=_rp(cfg, "cloud"))

        chosen: str | None = None
        pod: dict | None = None
        for attempt, candidate in enumerate(offered, start=1):
            if attempt > 1:
                note(f"  {offered[attempt - 2]} was refused; trying {candidate}")
            note(f"requesting {candidate}")
            try:
                pod = provider.create(**request_kwargs(candidate))
            except ProviderError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            chosen = candidate
            break

        if pod is None or chosen is None:
            # Nothing was created, so the row records an attempt and nothing else. Voiding keeps it
            # in the file with its reason: a row that disappears is a worse record than one that
            # says "no machine was ever made for this".
            ledger.close(note="create-failed-nothing-created", void=True)
            note("error: every preferred GPU was refused at creation:")
            for line in errors:
                note(f"  {line}")
            emit(action="refused", reason="create-failed", attempts=errors, gpu_preference=offered)
            return 3

        try:
            run = ledger.finalise(provisional["run_id"], instance_id=pod["id"], gpu_id=chosen,
                                  rate_usd_per_hour=float(pod.get("cost") or 0.0)
                                  or float(provisional.get("rate_usd_per_hour") or 0.0))
            if run is None:
                # The row is not ours any more — another process closed or replaced it. The machine
                # is billing with nothing recording it, which is the one state this tool must never
                # leave behind, so it is stopped rather than kept, and the row that is open (if any)
                # is closed: an open row accrues against the cap until someone closes it.
                note(f"error: the ledger row for {pod['id']} is gone; stopping the machine rather "
                     f"than leaving it billing with no record")
                _abandon_created(cfg, provider, ledger, pod)
                emit(action="unrecorded", instance={"id": pod.get("id"),
                                                    "status": pod.get("status")}, stopped=True)
                return 1
            state = _read_state(cfg)
            # A new machine is not an override of anything, and the cloud is recorded so a later
            # `up` can compare the config against this instance exactly rather than by card alone.
            state.pop("config_override", None)
            state.update({"instance_id": pod.get("id"), "provider": provider_name(cfg),
                          "gpu_id": chosen, "cloud": _rp(cfg, "cloud")})
            _write_state(cfg, state)
            note(f"instance       : {pod.get('id')}  ${pod.get('cost')}/h")
            pod = provider.wait_running(pod["id"], log=note)

            # Two consecutive alive readings, not one. `wait_running` returns the first payload that
            # merely looks alive; on the real account `up` reported RUNNING with uptime=None and
            # ports=NULL, exited 0, and `run` refused the machine seconds later. A machine that is
            # not usable is not a rental — so success is declared only once a second reading agrees.
            # The row already exists by then (it was opened before the machine), and this is the
            # point the two designs meet: a crash-looping machine is stopped, and the seconds it did
            # bill are closed out rather than voided or hidden.
            confirmed = provider.get(pod["id"]) or pod
            if not is_alive(confirmed):
                runtime = confirmed.get("runtime") or {}
                note(f"error: instance {pod.get('id')} reported {confirmed.get('status')} but is "
                     f"not usable on a second read (uptime={runtime.get('uptime')}, ports="
                     f"{'set' if runtime.get('ports') else 'NULL'}); no rental was confirmed, and "
                     f"the machine is being stopped so it cannot bill unrecorded")
                stopped_ok, detail = _stop_machine(cfg, provider, pod)
                if not stopped_ok:
                    note(f"  could not stop it ({detail}); it may still be billing — check `rentguard "
                         f"doctor`")
                closed = ledger.close(note="up-unusable", instance_status=confirmed.get("status"))
                if closed is not None:
                    note(f"run closed     : {closed['run_id']}  "
                         f"${closed['estimated_usd']}  (the seconds it existed are recorded)")
                emit(action="unusable", instance={"id": pod.get("id"),
                                                  "status": confirmed.get("status")},
                     stopped=True, run=_closed_dict(closed))
                return 1
            pod = confirmed
        except BaseException:
            # Interrupted, or the wait failed: the row is already open (that is the invariant), and
            # when the process is still alive the better outcome is a machine that stops too.
            _abandon_created(cfg, provider, ledger, pod)
            raise

        _print_access(cfg, provider, pod)

        note(f"\ncap            : ${cfg.budget.cap_usd:.2f} total, "
              f"${cfg.budget.per_run_cap_usd:.2f} per run, {cfg.budget.max_hours_per_run:.1f} h max")
        note("next           : `rentguard watch` in another terminal, or start it with nohup. "
              "Until something is watching, only you are.")
        emit(action="created", instance={"id": pod.get("id"), "status": pod.get("status"),
                                         "gpu": chosen, "cost_usd_per_hour": pod.get("cost")},
             run=_run_dict(ledger.open_run()),
             cap={"cap_usd": cfg.budget.cap_usd, "per_run_cap_usd": cfg.budget.per_run_cap_usd,
                  "max_hours_per_run": cfg.budget.max_hours_per_run})
    return 0


def _open_if_needed(cfg: Config, ledger: Ledger, pod: dict, purpose: str | None,
                    provider: Provider | None = None) -> dict | None:
    """Open a row for this instance, backdated from the provider's own uptime.

    An instance that has been billing for two hours is recorded at two hours, not at zero. The
    uptime is the provider's number, printed as such, and it is the only witness to money spent
    before the ledger existed.

    A row open against a *different* instance is stale (that machine vanished, this is its
    replacement), and is closed before the new row opens. Refusing instead would leave the machine
    that is billing now with no row at all — the live machine untracked and the cap check reading
    low. The row that ends up open always names the machine that is billing.
    """
    pod_id = pod.get("id")
    open_run = ledger.open_run()
    if open_run is not None:
        if open_run.get("instance_id") == pod_id:
            return open_run
        _release_open_run(ledger)
    uptime_seconds = float((pod.get("runtime") or {}).get("uptime") or 0.0)
    started = now_utc() - timedelta(seconds=uptime_seconds)
    run = ledger.open(
        purpose=purpose or "(unstated)",
        provider=provider.name if provider is not None else provider_name(cfg),
        instance_id=pod_id,
        gpu_id=(pod.get("gpu") or {}).get("id"),
        rate_usd_per_hour=float(pod.get("cost") or 0.0),
        started_at=started,
    )
    note(f"ledger row     : {run['run_id']}")
    if uptime_seconds > 60:
        note(f"backdated start: {run['started_at']}  (uptime {uptime_seconds / 3600:.2f} h from "
             f"the provider; ${ledger.live_usd():.2f} was already on the clock)")
    return run


def _release_open_run(ledger: Ledger) -> dict | None:
    """Close the open row, if there is one, because the instance in hand is not its instance.

    A row open against a machine that has vanished is money that was really spent; it is closed with
    its cost, and refusing instead would leave the machine billing now with no row at all.

    The exception is a **provisional** row that never reached a machine. It recorded an intention,
    not a rental, and the row that replaces it backdates to the machine's own start — so counting
    both would count the same minutes twice. It is voided, and the void keeps it in the file.
    """
    open_run = ledger.open_run()
    if open_run is None:
        return None
    if str(open_run.get("tag") or "") == PROVISIONAL_TAG and not open_run.get("instance_id"):
        closed = ledger.close(note="superseded-provisional", void=True)
    else:
        closed = ledger.close(note="superseded-open-run")
    if closed is not None:
        note(f"closed stale row: {closed['run_id']} (${closed['estimated_usd']}) was open "
             f"against {closed.get('instance_id')!r}; continuing with the instance in hand")
    return closed


def _estimated_rate(provider: Provider, gpu_id: str, cloud: str) -> float:
    """What the catalogue says this GPU costs, for a row that must exist before the machine does.

    A catalogue number is not a bill, and the pod's own rate replaces it seconds later. It is here
    so that an interruption between asking for a machine and being told its id cannot leave money
    that accrues at zero.
    """
    try:
        price = provider.price_of(gpu_id, cloud)
    except ProviderError:
        price = None
    return float(price or 0.0)


def _open_provisional(cfg: Config, ledger: Ledger, provider: Provider, gpu_id: str,
                      purpose: str | None, cloud: str) -> dict:
    """Open the rental row **before** the machine can exist.

    ``create`` is the irreversible step: from the moment it returns, a machine is billing. Opening
    the row after a confirmation that can take minutes left a window in which an interruption — a
    Ctrl-C, a dropped connection, a killed shell — produced a live pod that no ledger named. That is
    defect 10, found on the real account as pod ``2abx0hz2boe6ow``.

    So the row comes first, marked provisional because it names no instance yet, at the catalogue's
    rate and a few seconds *before* the machine starts billing. Over-counting by the create latency
    is the safe direction, and a row written before the irreversible step is the only guarantee that
    survives a SIGKILL. The gap it leaves is a row that names no machine, which `doctor` already
    reports as an error rather than treating as normal.
    """
    _release_open_run(ledger)
    run = ledger.open(purpose=purpose or "(unstated)", provider=provider.name, instance_id=None,
                      gpu_id=gpu_id, rate_usd_per_hour=_estimated_rate(provider, gpu_id, cloud),
                      tag=PROVISIONAL_TAG)
    note(f"ledger row     : {run['run_id']}  (provisional: opened before the machine exists)")
    return run


def _abandon_created(cfg: Config, provider: Provider, ledger: Ledger, pod: dict) -> None:
    """The way out of a `create` whose rental will not be confirmed: stop it, and close the row.

    Every step is best-effort and says what it did. Raising here would replace the exception that
    brought us here, and — the rule the shutdown path learned — a refused stop must not skip the
    ledger close: the cost is recorded either way, and a machine that is somehow still billing is
    then visible to `doctor` as a live instance with no open row, which is an error.

    **The machine must be this root's to stop.** A name that belongs to another root is that root's
    machine, recorded in that root's ledger, which this one cannot see; stopping it because *this*
    ledger has no row would kill their job on the strength of a missing local file. The row this
    closes is by construction in this root's ledger (``Ledger`` is built from this root's config), so
    the only thing left to check is the name.
    """
    instance_id = pod.get("id")
    if instance_claim(cfg, pod) == CLAIM_ANOTHER_ROOT:
        note(f"NOT stopping {instance_id}: its name {pod.get('name')!r} belongs to another rentguard "
             f"root, so that root's ledger is the one recording it, and stopping it here would kill "
             f"their job. The row opened here is voided below rather than left accruing.")
        row = ledger.close(note="machine-named-for-another-root", void=True)
        if row:
            note(f"row voided     : {row['run_id']} (a machine another root owns)")
        return
    stopped_ok, detail = _stop_machine(cfg, provider, pod)
    if stopped_ok:
        note(f"stopping {instance_id}: {detail}")
    else:
        note(f"could not stop {instance_id}: {detail}")
        note("the cost is recorded below anyway; the machine may still be billing — check "
             "`rentguard doctor`")
    row = ledger.close(note="up-interrupted", instance_status=pod.get("status"))
    if row:
        note(f"run closed     : {row['run_id']}  {row['wall_clock_hours']} h  "
             f"${row['estimated_usd']}")
    note(f"total spend    : ${ledger.total_usd():.2f}")


def _config_mismatch(cfg: Config, pod: dict, state: dict) -> list[str]:
    """The ways the recorded instance contradicts the config, each naming **both** facts.

    The config is the promise: a cloud tier and a card. A recorded instance may be reused or resumed,
    but substituting a different one silently is how `up` spent a window on a community 3080 Ti while
    the config asked for SECURE and an L40S — and then failed to start a stopped L40S at all
    ("not enough free GPUs on the host machine to start this pod", 2026-10-14). Naming both facts is
    the point: the defect was that the config was silently outvoted, so the message has to show what
    was outvoted by what.
    """
    reasons: list[str] = []
    wanted_cloud = str(_rp(cfg, "cloud") or "").upper()
    recorded_cloud = str(state.get("cloud") or "").upper()
    if wanted_cloud and recorded_cloud and recorded_cloud != wanted_cloud:
        reasons.append(f"cloud: the config asks {wanted_cloud}, this instance was rented as "
                       f"{recorded_cloud}")
    preference = [str(gpu) for gpu in (_rp(cfg, "gpu_preference") or [])]
    gpu = (pod.get("gpu") or {}).get("id")
    if preference and gpu and str(gpu) not in preference:
        reasons.append(f"gpu: the config asks {', '.join(preference)}, this instance is {gpu!r}")
    return reasons


def _print_access(cfg: Config, provider: Provider, pod: dict) -> None:
    try:
        argv, target = _ssh_argv(cfg, provider, pod)
    except ProviderError as exc:
        note(f"ssh            : (not available: {exc})")
        return
    note(f"ssh            : {' '.join(shlex.quote(a) for a in argv)} {target}")
    emit(ssh={"argv": argv, "target": target})


def _try_pull(cfg: Config, args) -> int:
    """Pull, and never raise.

    A pull that crashes must not skip whatever the caller owed the operator — the commonest case is
    `down`, where an exception between "pull" and "stop" leaves a machine billing because a tar
    stream failed. The money rule outranks the data rule: stop the machine, then report the pull.
    """
    try:
        return cmd_pull(argparse.Namespace(json=getattr(args, "json", False)))
    except Exception as exc:  # noqa: BLE001 - surviving any of them is the entire point
        note(f"pull failed before it could run: {type(exc).__name__}: {exc}")
        return 1


def cmd_down(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    with json_report(args):
        emit(command="down", action="noop", stopped=False)
        if pod is None:
            row = ledger.close(note="down-no-instance")
            note(f"no instance recorded; closed row {row['run_id']}" if row else "nothing to do")
            if row:
                emit(action="closed-row", run=_closed_dict(row),
                     total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
            else:
                emit(total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
            return 0
        status = pod.get("status")
        live = status not in ("EXITED", "TERMINATED") and is_alive(pod)
        pull_exit = None
        # Pull before the machine stops: an instance without a network volume is a container, and
        # once it is stopped its results are gone. A failed pull does not cancel the stop — leaving
        # a broken machine billing is the worse outcome — but it must be visible and the exit code
        # non-zero, so a script cannot read a lost pull as a clean stop.
        if cfg.artefacts.pull and live:
            note(f"pulling artefacts from {pod.get('id')} before stopping it")
            pull_exit = _try_pull(cfg, args)
            if pull_exit != 0:
                summary = _LAST_PULL or {}
                delivered = list(summary.get("files") or [])
                missing = list(summary.get("unmatched_patterns") or []) or list(cfg.artefacts.pull)
                # What the code can actually know: in the failing branch there is no remote listing,
                # so this names *patterns* the pull could not deliver — the word "paths" claimed a
                # listing that does not exist. And the sentence fires only when nothing arrived,
                # because "about to lose" is false once a file landed.
                if not delivered:
                    note(f"pull failed (exit {pull_exit}); stopping anyway — nothing was delivered, "
                         f"and these configured patterns may still hold files that will be lost: "
                         f"{', '.join(missing)}")
                else:
                    note(f"pull delivered {len(delivered)} file(s), but these configured patterns "
                         f"matched nothing: {', '.join(missing)} — the machine may still hold them")
                if getattr(args, "keep_running", False):
                    note(f"pull failed (exit {pull_exit}); --keep-running was given, so "
                          f"the machine stays up to retry. It is BILLING until you run "
                          f"`rentguard down` without --keep-running.")
                    note(f"patterns not delivered: {', '.join(missing)}")
                    emit(action="kept-running", stopped=False, kept_running=True,
                         instance={"id": pod.get("id"), "status": status},
                         pull_exit_code=pull_exit,
                         patterns_not_delivered=missing,
                         total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
                    return 1
                note("--keep-running would leave the machine up to retry; stopping is "
                     "the default because a machine that keeps billing is the worse loss.")

        stop_failed = False
        if live:
            stopped_ok, detail = _stop_machine(cfg, provider, pod)
            if stopped_ok:
                note(f"stopping {pod['id']}: {detail}")
            else:
                # The shutdown-path failure class, second instance: anything that raises between the
                # decision and the ledger close must not skip the close. The row is closed anyway,
                # and the exit code is non-zero.
                stop_failed = True
                note(f"could not stop {pod['id']}: {detail}")
                note("the row is still closed below, so the cost is recorded; the machine may still "
                     "be billing — check `rentguard doctor`")
        row = ledger.close(note="down", instance_status=status)
        if row:
            note(f"run closed     : {row['run_id']}  {row['wall_clock_hours']} h  "
                  f"${row['estimated_usd']}")
        note(f"total spend    : ${ledger.total_usd():.2f} of ${cfg.budget.cap_usd:.2f}")
        # The canonical instance shape carries ``volumeId``; ``volume`` was this command reading a
        # key no provider returns, so a machine with a network volume was never told it survives.
        if pod.get("volumeId") or pod.get("volume"):
            note("volume survives; the container disk does not.")
        emit(action="stop-failed" if stop_failed else ("stopped" if live else "already-stopped"),
             stopped=live and not stop_failed,
             instance={"id": pod.get("id"), "status": status},
             run=_closed_dict(row),
             pulled=(pull_exit == 0) if cfg.artefacts.pull and live else None,
             pull_exit_code=pull_exit,
             total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
    if stop_failed or pull_exit not in (None, 0):
        return 1
    return 0


def cmd_watch(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    interval = getattr(args, "interval", 60.0)
    once = bool(getattr(args, "once", False))
    max_iterations = 1 if once else getattr(args, "max_iterations", None)
    with json_report(args):
        emit(command="watch", interval_seconds=interval, once=once,
             max_iterations=max_iterations)
        # The armed state is read *before* this call polls. A watch writes a heartbeat of its own, so
        # asking afterwards would always report "armed" about a guard that has already returned —
        # the exact question an agent needs answered honestly.
        age_before = heartbeat_age_minutes(cfg.heartbeat_path)
        armed_before = age_before is not None and age_before <= max(2 * interval / 60.0, 2.0)
        emit(armed_before=armed_before, heartbeat_age_minutes=age_before)
        if pod is None:
            note("no instance recorded; nothing to watch")
            emit(reason="no-instance", armed=False)
            return 1
        note(f"watching {pod.get('id')} — stop at ${cfg.budget.per_run_cap_usd:.2f} this run, "
              f"${cfg.budget.cap_usd:.2f} total, {cfg.budget.max_hours_per_run:.1f} h, "
              f"or {cfg.budget.idle_minutes:.0f} min idle")
        reason = guard_watch(cfg, provider, ledger, instance_id=pod["id"], interval=interval,
                             log=note, max_iterations=max_iterations)
        stopped = reason not in ("guard-iteration-limit", "instance gone")
        note(f"guard finished: {reason}")
        if stopped:
            note(f"the guard stopped the machine: {reason}")
        else:
            # A bounded watch arms protection only while it runs. Say what is true on return.
            note("protection : NOT armed now — a watch stops when it returns, so run it again (or "
                 "keep `rentguard watch` running in a terminal) or the machine is unguarded")
        emit(reason=reason, instance_id=pod.get("id"), stopped=stopped, armed=False,
             armed_before=armed_before,
             total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
    return 0


def cmd_adopt(args) -> int:
    """Attach a ledger row to an instance that is already running.

    The start time is backdated from the provider's own uptime, so an instance that has been
    billing for hours is recorded at its real cost rather than starting the clock at zero.
    Starting the clock at zero is how a real overspend becomes invisible.
    """
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)
    with json_report(args):
        emit(command="adopt", instance_id=args.instance_id)
        pod = provider.get(args.instance_id)
        if pod is None:
            note(f"error: no instance {args.instance_id}")
            emit(adopted=False, reason="no-instance")
            return 1
        if ledger.open_run() is not None:
            note("error: a run is already open; close it first (`rentguard ledger` shows it)")
            emit(adopted=False, reason="run-already-open", open_run=_open_run_dict(ledger))
            return 1
        runtime = pod.get("runtime") or {}
        uptime = float(runtime.get("uptime") or 0.0)
        status = str(pod.get("status") or "").upper()
        if args.uptime_hours is not None:
            # For recording an instance after it was stopped, when the provider no
            # longer reports how long it ran. The source of the number is printed.
            uptime = float(args.uptime_hours) * 3600.0
            note(f"note           : uptime given as {args.uptime_hours} h, not read from the provider")
        elif uptime <= 0 and status in ("EXITED", "TERMINATED"):
            # No silent zero. An instance that has stopped reports uptime 0, so a row opened now
            # would start the clock at this moment and record ≈$0 for a machine that really ran.
            # A row that lies quietly is worse than a refusal that asks for the number.
            note(f"error: instance {pod.get('id')} is {status} and the provider reports no uptime, "
                 f"so what it cost cannot be recovered from it. Re-run with `--uptime-hours <hours>` "
                 f"(from the provider console or your own notes). Opening a row at $0 for a machine "
                 f"that ran would be a quieter lie than opening none.")
            emit(adopted=False, reason="uptime-unrecoverable", instance_id=pod.get("id"),
                 status=status)
            return 1
        started = now_utc() - timedelta(seconds=uptime)
        run = ledger.open(
            purpose=args.purpose or "(adopted, purpose unstated)",
            provider=provider.name,
            instance_id=pod.get("id"),
            gpu_id=(pod.get("gpu") or {}).get("id"),
            rate_usd_per_hour=float(pod.get("cost") or 0.0),
            tag="adopted",
            started_at=started,
        )
        state = _read_state(cfg)
        state.update({"instance_id": pod.get("id"), "provider": provider.name})
        _write_state(cfg, state)
        note(f"adopted        : {pod.get('id')}  {pod.get('status')}  ${pod.get('cost')}/h")
        note(f"row            : {run['run_id']}")
        note(f"backdated start: {run['started_at']}  (uptime {uptime / 3600:.2f} h from the provider)")
        note(f"cost already on the clock: ${ledger.live_usd():.2f} — this is spend that happened "
              f"before the ledger knew about it, and it is now recorded rather than hidden")
        emit(adopted=True, run=_run_dict(run), uptime_hours=round(uptime / 3600, 4),
             accrued_usd=ledger.live_usd(),
             instance={"id": pod.get("id"), "status": pod.get("status"),
                       "cost_usd_per_hour": pod.get("cost")})
    return 0


def _remote_command(command: list[str]) -> str:
    """The string handed to the remote shell, from the arguments the local shell already split.

    One argument is passed through **untouched**: the caller's own shell has already done whatever
    quoting it needed to do, so `rentguard run -- 'mkdir -p results; hostname > results/out.json'`
    arrives here as one word that must stay one word — a compound command the *remote* shell has to
    parse. Quoting it again sends the whole line as a single word, and bash then looks for a program
    literally named `mkdir -p results; hostname ...`.

    Several arguments are quoted individually, because they are separate words and any quote
    characters in them are data: `run -- python -c 'print(1)'` must keep its parentheses.
    """
    if not command:
        return ""
    if len(command) == 1:
        return command[0]
    return " ".join(shlex.quote(part) for part in command)


def _ssh_to_file(argv: list[str], target: str, remote: str, *, sink, timeout: float
                 ) -> tuple[int, bool]:
    """Run one ssh command with its stdout going to ``sink``: ``(exit code, did the bound expire)``.

    ``stdin`` is the null device on purpose. A route that answers a session without ever reading the
    command leaves the child waiting for input forever — measured 2026-10-14: `down`, which pulls
    before it stops, sat for over ten minutes while the machine billed, because its ssh had no stdin
    and no bound. With the null device the session sees EOF and returns.

    The bounded wait is the same protection one degree out: a pull is never worth a billing machine.
    When the bound expires the child is killed, and the caller is told so it can stop anyway.

    ``Popen`` rather than ``call`` for that reason: ``call`` cannot be bounded.
    """
    proc = subprocess.Popen([*argv, target, remote], stdout=sink, stdin=subprocess.DEVNULL)
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -1, True


def _stop_machine(cfg: Config, provider: Provider, pod: dict) -> tuple[bool, str]:
    """Stop a billing machine, bounded, with one escalation: ``(stopped, what happened)``.

    A stop is the one call whose failure has no ceiling — the machine bills for as long as it takes
    — so it runs on a worker thread that this function waits for with a bound. If the ordinary call
    has not come back by then, one fresh hurried call goes to the provider's stop endpoint
    (``force_stop``): that is what the operator did by hand on 2026-10-14 when a stuck route kept a
    machine open, and a hung socket must never be able to hold one open again.

    ``[budget] stop_timeout_s`` is the bound. The worker is a daemon, so a socket that never returns
    cannot stop this process from finishing its report either.
    """
    instance_id = pod.get("id")
    bound = float(getattr(cfg.budget, "stop_timeout_s", 45.0) or 45.0)
    outcome: dict[str, str] = {}

    def attempt() -> None:
        try:
            provider.action(instance_id, "stop")
            outcome["result"] = "stopped"
        except Exception as exc:  # noqa: BLE001 - the bound is the point, not the exception type
            outcome["result"] = f"failed: {type(exc).__name__}: {exc}"

    worker = threading.Thread(target=attempt, name=f"rentguard-stop-{instance_id}", daemon=True)
    worker.start()
    worker.join(bound)
    if not worker.is_alive():
        detail = outcome.get("result", "stopped")
        return detail == "stopped", detail
    try:
        provider.force_stop(instance_id)
        return True, f"escalated to the provider's stop endpoint after {bound:.0f}s"
    except Exception as exc:  # noqa: BLE001 - report it, do not raise over a dying machine
        return False, f"the stop call did not return in {bound:.0f}s and force_stop failed: {exc}"


def _tail(text: str, limit: int = RUN_OUTPUT_LIMIT) -> tuple[str, bool]:
    """The last ``limit`` characters of a command's output, and whether anything was dropped.

    The tail and not the head: a training run's last lines are its result, and a failure's last lines
    are its traceback. What was dropped is reported as a byte count, so a reader can tell a short log
    from a truncated one instead of assuming the whole story arrived.
    """
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _run_remote(provider: Provider, argv: list[str], target: str, remote: str,
                *, capture: bool) -> tuple[int, str, str]:
    """Run the command on the machine: ``(exit code, stdout, stderr)``.

    ``capture`` is set when a structured result is being collected, and it is the whole point: the
    child must not inherit our stdout, because under ``--json`` that stdout is the document, and for
    the MCP server it is the JSON-RPC protocol stream itself. A Python-level redirect cannot contain
    a child, so the streams are piped and returned in the result — which is also what a Bot wants,
    since previously the output was unreadable *and* in the way.

    Without ``capture`` nothing changes: the child inherits the terminal, and a person watching a
    training run sees it live. The provider's in-memory command seam is captured at the Python level,
    because that runner runs in this process rather than in a child.
    """
    if provider.command_runner is not None:
        if not capture:
            return provider.command_runner(argv, remote), "", ""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = provider.command_runner(argv, remote)
        return code, out.getvalue(), err.getvalue()

    if not capture:
        return subprocess.call([*argv, target, remote]), "", ""
    completed = subprocess.run([*argv, target, remote], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    return (completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"))


def cmd_run(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    jobs = JobLog(cfg.jobs_path)
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    workdir = args.workdir or cfg.artefacts.remote_dir
    with json_report(args):
        emit(command="run", workdir=workdir)
        if pod is None:
            note("error: nothing is rented. `rentguard up` first.")
            emit(ran=False, reason="nothing-rented")
            return 1
        _require_alive(provider, pod)

        command = list(args.command)
        while command and command[0] == "--":
            command.pop(0)
        if not command:
            note("error: nothing to run. Usage: rentguard run -- <command>")
            emit(ran=False, reason="no-command")
            return 1

        remote = _remote_command(command)
        argv, target = _ssh_argv(cfg, provider, pod)
        started = now_utc()
        note(f"$ {remote}  (in {workdir} on {pod.get('id')})")
        remote_command = f"cd {workdir} && {remote}"
        # Under a structured result the command's output is captured and returned, never inherited:
        # our stdout is the document, and for the MCP server it is the JSON-RPC stream. `note()` the
        # tail so a person still sees it, and put both streams in the result for the machine.
        capture = json_wanted(args)
        code, run_out, run_err = _run_remote(provider, argv, target, remote_command,
                                             capture=capture)
        shown_out, out_dropped = _tail(run_out)
        shown_err, err_dropped = _tail(run_err)
        if capture and (shown_out or shown_err):
            for name, text in (("stdout", shown_out), ("stderr", shown_err)):
                if text:
                    note(f"--- {name} ---")
                    for line in text.rstrip("\n").splitlines():
                        note(line)
            if out_dropped or err_dropped:
                note(f"output truncated: {len(run_out.encode())} bytes of stdout and "
                     f"{len(run_err.encode())} bytes of stderr were produced; the last "
                     f"{RUN_OUTPUT_LIMIT} characters of each are below")
        finished = now_utc()
        jobs.append({
            "run_id": (ledger.open_run() or {}).get("run_id"),
            "command": remote,
            "workdir": workdir,
            "exit_code": code,
            "started_at": iso(started),
            "finished_at": iso(finished),
            "seconds": round((finished - started).total_seconds(), 1),
        })

        pulled = None
        artefacts_lost = False
        pull_exit = None
        # As each run finishes is the only safe time to pull: an instance with no volume is a
        # container, and a restart discards everything on it. A failed run is exactly when the logs
        # and partial results are wanted, so this happens whatever the exit code.
        if cfg.artefacts.pull:
            # The provider is asked again, after the command: an instance that vanished mid-run
            # (a crash, a provider-side kill) has results that are now unrecoverable, and that must
            # not be reported as a successful run.
            still_there = provider.get(pod.get("id")) is not None
            if not still_there:
                artefacts_lost = True
                note(f"artefacts      : NOT pulled — instance {pod.get('id')} is no longer in "
                      f"the account, so {len(cfg.artefacts.pull)} configured pattern(s) went with "
                      f"it. The results for this run are lost.")
            else:
                pull_exit = _try_pull(cfg, args)

        # Whether the pull actually worked, not merely whether it was configured: claiming artefacts
        # were kept while the line above says "pull failed" is how a lost result reads as a kept one.
        pulled_ok = bool(cfg.artefacts.pull) and not artefacts_lost and pull_exit == 0
        if code != 0 or (pull_exit not in (None, 0)) or artefacts_lost:
            note(f"command exited {code}; artefacts "
                 f"{'pulled' if pulled_ok else 'NOT pulled'}")
        emit(ran=True, command=remote, exit_code=code,
             seconds=round((finished - started).total_seconds(), 1), pulled=pulled,
             artefacts_lost=artefacts_lost, pull_exit_code=pull_exit,
             stdout=shown_out, stderr=shown_err,
             stdout_bytes=len(run_out.encode()), stderr_bytes=len(run_err.encode()),
             truncated=bool(out_dropped or err_dropped))
    if artefacts_lost or (pull_exit not in (None, 0)):
        return 1
    return code


# ------------------------------------------------------------------ artefacts

def _copy_matching(members: dict[str, bytes], dest: Path) -> list[str]:
    """Write matched files under ``dest``, refusing to escape it.

    Files from a rented machine are untrusted input: a member named ``../../.ssh/authorized_keys``
    must not be able to leave the destination directory.
    """
    written: list[str] = []
    root = dest.resolve()
    for name, data in members.items():
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"refusing to write outside {root}: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(name)
    return written


@contextlib.contextmanager
def _temp_tar(directory: Path | None = None):
    """A file to hold the tar stream, instead of a pipe to the child.

    Piping a grandchild's stdout needs two processes alive at once and is refused in some sandboxes.
    A temporary file is one syscall less clever and always works — but not necessarily in the system
    temp dir, which is read-only under some confinements and ``mkdtemp``-owned (mode 0700) under
    others. So the caller passes a directory it knows is writable (the project's state dir), and the
    system temp is only the fallback.
    """
    import shutil
    import tempfile
    import uuid

    bases = [d for d in (directory, Path(tempfile.gettempdir())) if d is not None]
    path: Path | None = None
    last_error: OSError | None = None
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / f".rentguard-pull-{uuid.uuid4().hex}"
            candidate.mkdir()
            path = candidate
            break
        except OSError as exc:
            last_error = exc
    if path is None:
        raise last_error or OSError("no writable directory for the pull archive")

    archive = path / "artefacts.tar.gz"
    try:
        with archive.open("wb") as handle:
            yield handle, archive
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _unmatched_patterns(patterns: list[str], names: list[str]) -> list[str]:
    """The configured patterns that no archived member actually answers.

    A pattern counts as answered only when an extracted member **matches it** — ``fnmatch`` against
    the member path, with ``**/`` also tried collapsed, because ``fnmatch`` has no recursive-wildcard
    semantics and ``results/**/*.json`` should still be answered by ``results/out.json``.

    A literal-prefix comparison is weaker than this in the dangerous direction: with only
    ``results/notes.txt`` delivered, the prefix ``results/`` answers ``results/*.json``, and a run
    whose JSON never arrived reads as a clean pull with nothing flagged. The prefix form is only a
    hint in the tool description now, never the answer.
    """
    unmatched: list[str] = []
    for pattern in patterns:
        text = str(pattern)
        collapsed = text.replace("**/", "")
        answered = any(fnmatch.fnmatch(str(name), text)
                       or fnmatch.fnmatch(str(name), collapsed)
                       for name in names)
        if not answered:
            unmatched.append(pattern)
    return unmatched


def _pull_remote_command(remote_dir: str, patterns: list[str], *, pty: bool) -> str:
    """The command the remote shell runs to hand the archive back.

    A direct endpoint is a plain byte channel, so the tar stream goes straight down it: that is the
    shortest path, and it is the one every secure-cloud rental used. A PTY is a *text* channel — it
    rewrites LF as CRLF and folds the remote stderr into stdout — so there the archive is written to
    a file on the machine, base64-encoded out of it, and decoded again on this side. A gzip stream
    cannot survive the translation; measured on a community pod, 2026-10-14, the result was
    ``ReadError: not a gzip file``.

    The remote tar's exit status is carried out with ``rc=$?`` rather than through a pipe, because a
    pipeline reports the status of its *last* command: with ``tar czf - … | base64`` a tar that
    failed on a missing member still exits 0 overall, and the empty archive that arrives would be
    reported as a successful pull of nothing. A lost result read as a kept one is the failure this
    whole path exists to avoid.
    """
    members = " ".join(patterns)
    if not pty:
        return f"cd {remote_dir} && tar czf - {members}"
    tmp = "/tmp/.rentguard-pull-$$.tar.gz"
    return (f"cd {remote_dir} && tar czf {tmp} {members}; rc=$?; "
            f"if [ $rc -eq 0 ]; then base64 {tmp}; fi; rm -f {tmp}; exit $rc")


def _decode_pty_stream(archive: Path) -> Path:
    """Turn the base64 text a PTY session delivered back into the bytes it encoded.

    Decoded in blocks rather than in one read: the archive may be far larger than is comfortable to
    hold in memory, and this is the path a trained model's results come down. Whitespace is dropped
    (a PTY turns every LF into CRLF, and base64 wraps its own lines), and any other non-base64
    character is an error rather than something to skip — quietly dropping characters out of an
    archive is how a corrupt stream decodes into a partial one that looks fine.
    """
    decoded = archive.with_name(archive.name + ".decoded")
    remainder = b""
    with archive.open("rb") as src, decoded.open("wb") as dst:
        while True:
            block = src.read(1 << 16)
            if not block:
                break
            buffer = remainder + b"".join(block.split())
            whole = len(buffer) - (len(buffer) % 4)
            if whole:
                try:
                    dst.write(base64.b64decode(buffer[:whole], validate=True))
                except binascii.Error as exc:
                    raise ValueError(f"the session's output is not base64 ({exc})") from exc
            remainder = buffer[whole:]
    if remainder:
        raise ValueError(f"the session's output ends inside a base64 group ({len(remainder)} "
                         f"characters)")
    return decoded


def cmd_pull(args) -> int:
    cfg = _config()
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    patterns = cfg.artefacts.pull
    # A caller that holds only the exit code (`down`) reads this, so it is set on every path.
    _pull_summary(pulled=False, reason="not-attempted", files=[], unmatched_patterns=[])
    with json_report(args):
        emit(command="pull", patterns=list(patterns),
             local_dir=str(Path(cfg.artefacts.local_dir)))
        if pod is None:
            note("error: nothing is rented")
            emit(pulled=False, reason="nothing-rented", files=[])
            return 1
        if not patterns:
            note("nothing configured to pull ([artefacts].pull is empty)")
            emit(pulled=False, reason="nothing-configured", files=[])
            return 0
        _require_alive(provider, pod)

        dest = Path(cfg.artefacts.local_dir)
        try:
            members = provider.pull_artifacts(pod, patterns)
        except NotImplementedError:
            members = None
        if members is not None:
            # A provider that hands over artefacts itself (the in-memory fake) does so here.
            written = _copy_matching(members, dest)
            note(f"pulled {len(written)} file(s) into {dest}")
            for name in written[:20]:
                note(f"  {name}")
            if len(written) > 20:
                note(f"  ... and {len(written) - 20} more")
            emit(pulled=True, files=written)
            return 0

        argv, target = _ssh_argv(cfg, provider, pod)
        pty = _ssh_pty(provider, pod)
        # A pull is bounded: it runs before `down` stops a machine, so a pull that never returns is a
        # bill that never stops. See `_ssh_to_file`.
        timeout = float(getattr(cfg.artefacts, "pull_timeout_s", 120.0) or 120.0)
        with _temp_tar(cfg.state_dir) as (handle, archive):
            remote = _pull_remote_command(cfg.artefacts.remote_dir, patterns, pty=pty)
            code, timed_out = _ssh_to_file(argv, target, remote, sink=handle, timeout=timeout)
            handle.flush()
            if timed_out:
                note(f"pull failed: ssh did not return within {timeout:.0f}s "
                     f"([artefacts] pull_timeout_s), so the pull was killed — a pull must never be "
                     f"able to hold a billing machine open")
                emit(pulled=False, reason="ssh-timeout", exit_code=None, files=[],
                     timeout_s=timeout)
                return 1
            if pty:
                # What landed in the file is base64 text, not the archive: decode it back. Every
                # failure here is reported, never raised, because `down` pulls through this function
                # and an exception between "pull" and "stop" leaves a machine billing.
                try:
                    archive = _decode_pty_stream(archive)
                except ValueError as exc:
                    note(f"pull failed: the stream from {pod.get('id')} is not an archive "
                         f"({archive.stat().st_size} bytes): {exc}")
                    emit(pulled=False, reason="stream-not-base64", exit_code=code, files=[])
                    return 1
            dest.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(archive, mode="r:gz") as tar:
                    names = tar.getnames()
                    tar.extractall(dest, filter="data")
            except (tarfile.TarError, OSError) as exc:
                if code != 0:
                    # tar's own status with nothing usable behind it: the configured patterns matched
                    # nothing at all. That is a named reason, not an ssh failure — and it is the one
                    # case where there is genuinely nothing to deliver.
                    note(f"pull failed: tar exited {code} and no member was archived — the configured "
                         f"patterns matched nothing ({', '.join(patterns)})")
                    emit(pulled=False, reason="patterns-matched-nothing", exit_code=code, files=[],
                         unmatched_patterns=list(patterns))
                    return 1
                # ssh exited 0, so the remote command ran — but what came back is not an archive.
                # On the real account that was the proxy's "your ssh client doesn't support PTY"
                # sentence, and tarfile's ReadError used to escape this command as a traceback.
                size = archive.stat().st_size
                head = archive.read_bytes()[:80]
                note(f"pull failed: the stream from {pod.get('id')} is not an archive "
                     f"({size} bytes, starting {head!r}): {exc}")
                emit(pulled=False, reason="stream-not-an-archive", exit_code=1, files=[],
                     bytes=size, head=head.decode("utf-8", "replace"))
                return 1
            if not names:
                # An archive with no members is not a delivery of zero files: the patterns matched
                # nothing on the machine, and the artefacts may still be sitting on it. Naming this
                # as the failure it is, is what stops `down` from quietly stopping the container that
                # holds them — `down` names the paths it is about to lose either way.
                note(f"pull failed: the remote tar archived no member — the configured patterns "
                     f"matched nothing ({', '.join(patterns)}); the files may still be on the machine")
                _pull_summary(pulled=False, reason="patterns-matched-nothing", files=[],
                              unmatched_patterns=list(patterns))
                emit(pulled=False, reason="patterns-matched-nothing", exit_code=code, files=[],
                     unmatched_patterns=list(patterns))
                return 1
            # GNU tar writes the members it did match and exits non-zero for the patterns it could
            # not. The results are in hand, so the pattern is the finding and the pull is not a
            # failure: reading the exit code alone lost a successful run's only copy of its results
            # and then let the container holding them be stopped (measured 2026-10-14).
            unmatched = _unmatched_patterns(patterns, names)
            _pull_summary(pulled=True, reason=None, files=list(names), unmatched_patterns=unmatched)
            if code != 0:
                note(f"tar exited {code}: at least one configured pattern matched nothing, and the "
                     f"members that did match are extracted")
            for pattern in unmatched:
                note(f"matched nothing : {pattern}")
        note(f"pulled {len(names)} file(s) into {dest}")
        for name in names[:20]:
            note(f"  {name}")
        if len(names) > 20:
            note(f"  ... and {len(names) - 20} more")
        emit(pulled=True, files=list(names), unmatched_patterns=unmatched, tar_exit_code=code)
    return 0


# ------------------------------------------------------------------ entry

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rentguard", description="Rent a GPU, cap the spend, keep the ledger, keep the results.")
    parser.add_argument("--version", action="version", version=f"rentguard {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def structured(p):
        p.add_argument("--json", action="store_true",
                       help="print one JSON document on stdout instead of text")
        return p

    structured(sub.add_parser("doctor", help="config, key, live instances, spend, drift")
               ).set_defaults(func=cmd_doctor)

    sp = sub.add_parser("init", help="write a commented rentguard.toml")
    sp.add_argument("path", nargs="?", default=None,
                    help="where to write it (default ./rentguard.toml)")
    sp.add_argument("--force", action="store_true", help="overwrite an existing file")
    sp.add_argument("--print", dest="print", action="store_true",
                    help="print the template instead of writing it")
    structured(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("up", help="rent (or resume) an instance — spends money")
    sp.add_argument("--purpose", default=None, help="what this rental is for; goes in the ledger")
    sp.add_argument("--gpu", default=None)
    sp.add_argument("--cap", type=float, default=None, help="total cap for this project")
    sp.add_argument("--per-run-cap", type=float, default=None)
    sp.add_argument("--force", action="store_true", help="ignore an already-reached cap")
    sp.add_argument("--dry-run", action="store_true",
                    help="check the cap and the GPU choice, rent nothing")
    structured(sp)
    sp.set_defaults(func=cmd_up)

    structured(sub.add_parser("status", help="uptime, spend, guard heartbeat")).set_defaults(
        func=cmd_status)

    sp = sub.add_parser("watch", help="foreground supervisor: stop at the cap or on idle")
    sp.add_argument("--interval", type=float, default=60.0)
    sp.add_argument("--once", action="store_true",
                    help="take one reading and report the decision; does not keep a guard alive")
    sp.add_argument("--max-iterations", type=int, default=None,
                    help="bounded: stop after N polls instead of running until the cap or a stop")
    structured(sp)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("down", help="stop the instance and close the ledger row")
    sp.add_argument("--keep-running", action="store_true",
                    help="if the artefact pull fails, leave the machine running so the copy "
                         "can be retried (it keeps billing). The default is to stop and "
                         "report which paths were lost, because a machine that keeps "
                         "billing is the worse loss")
    structured(sp).set_defaults(func=cmd_down)
    structured(sub.add_parser("ledger", help="every rental with its cost")).set_defaults(
        func=cmd_ledger)
    structured(sub.add_parser("pull", help="pull the configured artefacts now")).set_defaults(
        func=cmd_pull)

    sp = sub.add_parser("adopt", help="record an instance that is already running, backdated")
    sp.add_argument("instance_id")
    sp.add_argument("--purpose", default=None)
    sp.add_argument("--uptime-hours", type=float, default=None,
                    help="when the provider no longer reports uptime (already-stopped instance)")
    structured(sp)
    sp.set_defaults(func=cmd_adopt)

    sp = sub.add_parser("run", help="run a command on the instance, then pull artefacts")
    sp.add_argument("--workdir", default=None)
    structured(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        return 0 if result is None else int(result)
    except (ConfigError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
