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
- **After renting**, the row is backdated from the provider's own uptime, so an instance that has
  already been billing for hours is recorded at its real cost instead of starting the clock at
  zero. Starting the clock at zero is how overspend becomes invisible.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
from datetime import timedelta
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, api_key, load
from .guard import (
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

# The JSON document a command prints is accumulated here, so a command body reads as ordinary
# printing code and the structured form is assembled in one place.
_REPORT: dict | None = None


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
    root = Path(os.environ.get("RENTGUARD_ROOT") or Path.cwd()).resolve()
    tag = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"rentguard-{tag}"


def _ssh(fmt: str) -> str:
    """Windows has no /dev/null; the known-hosts file has to point at NUL there."""
    return fmt.format(null="NUL" if os.name == "nt" else "/dev/null")


def _ssh_argv(cfg: Config, provider: Provider, pod: dict) -> tuple[list[str], str]:
    """The ssh command line for this instance.

    Access is a provider method, not a RunPod static: a second provider may reach its machines by a
    different route entirely, and the caller must not have to know which one it is holding. A
    provider that does not implement the interface falls back to the RunPod shape, which is the
    payload every provider is required to return anyway.
    """
    key = Path(cfg.ssh_key) if cfg.ssh_key else None
    access = getattr(provider, "ssh_argv", None)
    if access is None:
        args, target = RunPod.ssh_args(pod, key)
    else:
        args, target = access(pod, key)
    args = [_ssh(a) for a in args]
    args = [cfg.ssh_binary, *args]
    # ssh wants the port before the target, which the provider has already arranged.
    return args, target


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
        "# balance_usd would be an OPTIONAL local assumption, never a reading. No provider exposes a",
        "# reliable balance, so rentguard never reports one: it reports spend against your cap.",
        "",
        "[artefacts]",
        "# Pulled when each `run` finishes and again on `down`, before the machine stops. An",
        "# instance with no network volume is a container: restart it and these files are gone.",
        'pull = ["results/**/*.json", "logs/*.log"]',
        f"local_dir = {_toml_str(a.local_dir)}",
        f"remote_dir = {_toml_str(a.remote_dir)}",
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
        note(f"spend recorded : ${ledger.total_usd():.2f} of a ${cfg.budget.cap_usd:.2f} cap "
              f"({len(ledger.entries())} run(s))")
        emit(spend={"recorded_usd": ledger.total_usd(), "cap_usd": cfg.budget.cap_usd,
                    "runs": len(ledger.entries()),
                    "open_run_accrued_usd": ledger.live_usd()})
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
            print()
            for finding in findings:
                note(f"  {finding.severity.upper():<5} {finding.code}: {finding.message}")
            emit(findings=_findings_dicts(findings), live_instances=[])
            return 1

        try:
            provider = _provider(cfg)
        except SystemExit as exc:
            note(f"provider       : {exc}")
            findings.append(Finding(code="provider-unavailable", severity="error",
                                    message=str(exc)))
            emit(findings=_findings_dicts(findings), live_instances=[])
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

        findings.extend(state_findings(state, open_run, provider_gate=probe))
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
                              "cost_usd_per_hour": p.get("cost"),
                              "gpu": (p.get("gpu") or {}).get("id"),
                              "tracked": p.get("id") == tracked} for p in live])
        for pod in live:
            known = pod.get("id") == tracked
            note(f"  {pod.get('id')}  {str(pod.get('status')):<12} ${pod.get('cost')}/h  "
                  f"{str((pod.get('gpu') or {}).get('id')):<28} "
                  f"{'tracked' if known else 'UNTRACKED'}")
        if not live:
            note("  (nothing is billing)")

        untracked = [p for p in live if p.get("id") != tracked]
        if untracked:
            findings.append(Finding(
                code="live-instance-no-ledger-row", severity="error",
                message=f"{len(untracked)} live instance(s) with no ledger row: "
                        f"{', '.join(str(p.get('id')) for p in untracked)}. Either `rentguard "
                        f"adopt <id> --purpose ...` or stop them."))
        if pods_error:
            findings.append(Finding(code="provider-error", severity="warn",
                                    message=f"could not list account pods: {pods_error}"))

        emit(findings=_findings_dicts(findings))
        if findings:
            print()
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
            note(f"ledger         : NO ROW for this instance — ${snap.run_usd:.3f} has been spent "
                  f"with nothing recording it (`rentguard adopt {pod.get('id')}`)")
        note(f"this run       : ${snap.run_usd:.3f} of ${snap.per_run_cap_usd:.2f}")
        line = f"all runs       : ${snap.spent_usd:.3f} of ${snap.cap_usd:.2f}"
        # Time-to-cap is only meaningful for a machine that is actually billing.
        if snap.status == "RUNNING" and snap.minutes_to_cap:
            line += f"  (~{snap.minutes_to_cap} min to the cap at this rate)"
        note(line)
        if age is None:
            note("guard          : NO HEARTBEAT — nothing is watching this instance")
        elif age > 5:
            note(f"guard          : heartbeat {age:.1f} min old — the guard is probably dead; "
                  f"nothing is watching this instance")
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
            return 0
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

        open_run = ledger.open_run()
        # An open row whose instance has vanished still represents money. Renting on top of it is
        # how a second bill appears, so the spend on that row is priced from its start time and
        # checked against the cap before anything is created.
        if open_run is not None and open_run.get("instance_id"):
            hidden = ledger.live_usd()
            if hidden >= cfg.budget.cap_usd and not args.force:
                note(f"error: run {open_run.get('run_id')} has already accrued ${hidden:.2f} "
                      f"against a ${cfg.budget.cap_usd:.2f} cap, and its instance is not visible. "
                      f"Close it with `rentguard down` first.")
                emit(action="refused", reason="cap-reached-open-run",
                     open_run=_open_run_dict(ledger))
                return 2

        pod = _pod(provider, cfg)

        if pod is not None:
            status = pod.get("status")
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
            note("error: every preferred GPU was refused at creation:")
            for line in errors:
                note(f"  {line}")
            emit(action="refused", reason="create-failed", attempts=errors, gpu_preference=offered)
            return 3

        if getattr(args, "dry_run", False):
            note(f"dry run: would request {chosen}; nothing was rented and nothing was spent")
            emit(action="dry-run", gpu_id=chosen, instance_id=None)
            return 0

        state = _read_state(cfg)
        state.update({"instance_id": pod.get("id"), "provider": provider_name(cfg),
                      "gpu_id": chosen})
        _write_state(cfg, state)
        note(f"instance       : {pod.get('id')}  ${pod.get('cost')}/h")
        pod = provider.wait_running(pod["id"], log=lambda m: print(m, flush=True))
        _open_if_needed(cfg, ledger, pod, args.purpose, provider)
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
        closed = ledger.close(note="superseded-open-run")
        if closed is not None:
            print(f"closed stale row: {closed['run_id']} (${closed['estimated_usd']}) was open "
                  f"against {closed.get('instance_id')!r}; this instance is {pod_id!r}")
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
    print(f"ledger row     : {run['run_id']}")
    if uptime_seconds > 60:
        print(f"backdated start: {run['started_at']}  (uptime {uptime_seconds / 3600:.2f} h from "
              f"the provider; ${ledger.live_usd():.2f} was already on the clock)")
    return run


def _print_access(cfg: Config, provider: Provider, pod: dict) -> None:
    try:
        argv, target = _ssh_argv(cfg, provider, pod)
    except ProviderError as exc:
        print(f"ssh            : (not available: {exc})")
        return
    print(f"ssh            : {' '.join(shlex.quote(a) for a in argv)} {target}")
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
                lost = ", ".join(cfg.artefacts.pull)
                if getattr(args, "keep_running", False):
                    note(f"pull failed (exit {pull_exit}); --keep-running was given, so "
                          f"the machine stays up to retry. It is BILLING until you run "
                          f"`rentguard down` without --keep-running.")
                    note(f"paths not pulled: {lost}")
                    emit(action="kept-running", stopped=False, kept_running=True,
                         instance={"id": pod.get("id"), "status": status},
                         pull_exit_code=pull_exit,
                         paths_not_pulled=list(cfg.artefacts.pull),
                         total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
                    return 1
                note(f"pull failed (exit {pull_exit}); stopping anyway — the container "
                      f"holds files that will be lost: {lost}")
                note("--keep-running would leave the machine up to retry; stopping is "
                     "the default because a machine that keeps billing is the worse loss.")

        if live:
            provider.action(pod["id"], "stop")
            note(f"stopping {pod['id']}")
        row = ledger.close(note="down", instance_status=status)
        if row:
            note(f"run closed     : {row['run_id']}  {row['wall_clock_hours']} h  "
                  f"${row['estimated_usd']}")
        note(f"total spend    : ${ledger.total_usd():.2f} of ${cfg.budget.cap_usd:.2f}")
        if pod.get("volume"):
            note("volume survives; the container disk does not.")
        emit(action="stopped" if live else "already-stopped",
             stopped=live,
             instance={"id": pod.get("id"), "status": status},
             run=_closed_dict(row),
             pulled=(pull_exit == 0) if cfg.artefacts.pull and live else None,
             pull_exit_code=pull_exit,
             total_usd=ledger.total_usd(), cap_usd=cfg.budget.cap_usd)
    if pull_exit not in (None, 0):
        return 1
    return 0


def cmd_watch(args) -> int:
    cfg = _config()
    ledger = Ledger(cfg.ledger_path)
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    interval = getattr(args, "interval", 60.0)
    with json_report(args):
        emit(command="watch", interval_seconds=interval)
        if pod is None:
            note("no instance recorded; nothing to watch")
            emit(reason="no-instance")
            return 1
        note(f"watching {pod.get('id')} — stop at ${cfg.budget.per_run_cap_usd:.2f} this run, "
              f"${cfg.budget.cap_usd:.2f} total, {cfg.budget.max_hours_per_run:.1f} h, "
              f"or {cfg.budget.idle_minutes:.0f} min idle")
        reason = guard_watch(cfg, provider, ledger, instance_id=pod["id"], interval=interval,
                    log=note)
        note(f"guard finished: {reason}")
        emit(reason=reason, instance_id=pod.get("id"),
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
        if args.uptime_hours is not None:
            # For recording an instance after it was stopped, when the provider no
            # longer reports how long it ran. The source of the number is printed.
            uptime = float(args.uptime_hours) * 3600.0
            note(f"note           : uptime given as {args.uptime_hours} h, not read from the provider")
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
        if provider.command_runner is not None:
            # In-memory provider: the command is executed by whatever supplied the runner.
            code = provider.command_runner(argv, remote_command)
        else:
            code = subprocess.call([*argv, target, remote_command])
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
             artefacts_lost=artefacts_lost, pull_exit_code=pull_exit)
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


def cmd_pull(args) -> int:
    cfg = _config()
    provider = _provider(cfg)
    pod = _pod(provider, cfg)
    patterns = cfg.artefacts.pull
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
        remote = f"cd {cfg.artefacts.remote_dir} && tar czf - {' '.join(patterns)}"
        with _temp_tar(cfg.state_dir) as (handle, archive):
            code = subprocess.call([*argv, target, remote], stdout=handle)
            handle.flush()
            if code != 0:
                note(f"pull failed (ssh exited {code})")
                emit(pulled=False, reason="ssh-failed", exit_code=code, files=[])
                return 1
            dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, mode="r:gz") as tar:
                names = tar.getnames()
                tar.extractall(dest, filter="data")
        note(f"pulled {len(names)} file(s) into {dest}")
        for name in names[:20]:
            note(f"  {name}")
        if len(names) > 20:
            note(f"  ... and {len(names) - 20} more")
        emit(pulled=True, files=list(names))
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
