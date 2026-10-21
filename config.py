"""Configuration for rentguard.

Search order for the project file:
    ./rentguard.toml
    ~/.config/rentguard/config.toml

The budget section is the point of the tool, so it is parsed into a dataclass and validated
eagerly: a negative cap or an idle window of zero is a mistake worth failing on at startup,
not at 3am when the guard is deciding whether to stop a machine.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class Budget:
    cap_usd: float = 25.0
    per_run_cap_usd: float = 5.0
    max_hours_per_run: float = 6.0
    idle_minutes: float = 10.0
    balance_usd: float = 0.0  # OPTIONAL local assumption; never presented as a reading
    #: How long a stop call may take before one fresh call is made to the provider's stop endpoint
    #: instead. A socket that hangs must not be able to hold a machine open: measured 2026-10-14,
    #: a stuck route kept a machine billing while the operator stopped it by hand.
    stop_timeout_s: float = 45.0
    #: How long one `run` may take before its session is killed and reported as bounded. This is not
    #: a limit on the work: it exists because a *dropped* session never returns at all — measured
    #: 2026-10-15, `run --json` on the community route hung past 120 s while the machine billed and
    #: nothing ran. A legitimate job that needs longer sets this higher.
    run_timeout_s: float = 3600.0
    #: How long `down` waits for the account to stop listing a machine as billing before it refuses
    #: to call the window closed. A stop call that returns is not a machine that stopped: measured
    #: 2026-10-16, `down` printed `run closed …`, exited 0, and the pod stayed RUNNING for minutes
    #: while `doctor` showed it live and unrecorded. The row stays open in that case, so the spend
    #: keeps being recorded and the cap keeps seeing it.
    stop_confirm_s: float = 30.0

    def __post_init__(self) -> None:
        if self.cap_usd <= 0:
            raise ConfigError("budget.cap_usd must be > 0")
        if self.per_run_cap_usd <= 0:
            raise ConfigError("budget.per_run_cap_usd must be > 0")
        if self.per_run_cap_usd > self.cap_usd:
            raise ConfigError("budget.per_run_cap_usd is larger than budget.cap_usd")
        if self.max_hours_per_run <= 0:
            raise ConfigError("budget.max_hours_per_run must be > 0")
        if self.idle_minutes < 0:
            raise ConfigError("budget.idle_minutes must be >= 0")
        if self.run_timeout_s < 0:
            raise ConfigError("budget.run_timeout_s must be >= 0 (0 means: do not bound a run)")
        if self.stop_confirm_s < 0:
            raise ConfigError("budget.stop_confirm_s must be >= 0")


#: A pull's bound is never shorter than this, whatever the floor and the volume say: a child that is
#: killed before a connection can complete is a pull that cannot succeed (measured 2026-10-17: a
#: `pull_timeout_s = 0` floor with a zero-byte selection killed a command that exited 0 in 0.2 s).
PULL_MIN_BOUND_S = 5.0
#: And never longer than this: a bound beyond a day is not a bound, it is a pull that outlives the
#: machine it bills. A volume that would need longer is reported with the bound it was given.
PULL_MAX_BOUND_S = 86_400.0
#: The largest inventory size accepted as a measurement. Above this, the bound's arithmetic stops
#: being meaningful (the integer is not convertible to a float): measured 2026-10-17, a readable
#: inventory line naming a 10**400-byte file raised `OverflowError` out of `pull`.
PULL_MAX_BYTES = 1 << 53
#: The characters a configured artefact pattern may contain. The inventory expands the pattern in the
#: **remote shell**, so anything the shell would interpret — whitespace, quotes, `;`, `$`, backticks,
#: `<`, `>`, `|`, `&`, `(`, `)`, backslashes — is refused at load rather than executed on the machine
#: (measured 2026-10-17: the archive quoted its exact paths while the inventory interpolated the raw
#: pattern). Globs need only these.
_PATTERN_SAFE = re.compile(r"^[A-Za-z0-9_./*?\[\]@%+,:=~-]+$")


@dataclass
class Artefacts:
    pull: list[str] = field(default_factory=list)
    local_dir: str = "./artefacts"
    remote_dir: str = "/workspace"
    #: The **floor** of one ssh pull's bound, in seconds. It is no longer the whole bound: a pull is
    #: bounded by the **volume it will carry** divided by ``pull_bytes_per_second`` (plus a slack), and
    #: this value is what a small pull still gets. Measured 2026-10-17: a run directory is ~700 MB and
    #: the 120 s default produced 26 timeouts in one window — the bound was a legacy constant, not a
    #: size. The pull runs before `down` stops a machine, so an unbounded pull is an unbounded bill.
    pull_timeout_s: float = 120.0
    #: The throughput the bound assumes, in bytes per second. Conservative on purpose: the community
    #: route measured far slower than a direct endpoint, and a bound that is too generous only makes a
    #: stuck pull cost more time, while one that is too tight loses a result that was still arriving.
    pull_bytes_per_second: float = 1_000_000.0
    #: Patterns that name a **collection of runs** — one directory per run, `grok/runs/*`. A declared
    #: collection carries only its **newest** entry each pull (the run just finished) and names the rest
    #: as skipped, with `rentguard pull --all` as the backstop. **Undeclared patterns carry everything
    #: they match**, because a glob cannot be told apart from one run's contents by its shape — and
    #: carrying the wrong half of a run is the loss this whole tool exists to prevent (Gilbert's ruling,
    #: 2026-10-17). Every entry must also be in ``pull``; a declaration naming nothing to pull is a
    #: config error rather than a silent no-op.
    run_patterns: list[str] = field(default_factory=list)
    #: There is deliberately no switch here. If ``pull`` is non-empty, `run` pulls when each run
    #: finishes and `down` pulls before it stops. The old ``on_run`` setting selected between those
    #: and doing nothing; it is deleted rather than defaulted, because a tool whose promise is "no
    #: surprises" cannot carry a setting that turns the promise off. A config that still has the key
    #: is accepted and the key ignored — see ``_only``.

    def __post_init__(self) -> None:
        if not math.isfinite(self.pull_timeout_s) or self.pull_timeout_s < 0:
            # nan and inf are legal TOML floats and both pass `< 0`; a nan bound makes `wait` never
            # expire, which is the protection this number exists to provide (attack, 2026-10-17).
            raise ConfigError("artefacts.pull_timeout_s must be a finite number of seconds >= 0")
        if not math.isfinite(self.pull_bytes_per_second) or self.pull_bytes_per_second <= 0:
            raise ConfigError("artefacts.pull_bytes_per_second must be a finite number of bytes per "
                              "second > 0 — a pull bound cannot assume an infinite throughput")
        for pattern in [*self.pull, *self.run_patterns]:
            text = str(pattern)
            if not text or not _PATTERN_SAFE.fullmatch(text):
                # `fullmatch`, not `match` with `$`: `$` also matches before a trailing newline, so a
                # pattern ending in "\n" passed the first version of this check (attack finding,
                # 2026-10-17) — and a newline in a remote shell command is a second command.
                raise ConfigError(
                    f"artefacts pattern {text!r} contains characters the remote shell would "
                    f"interpret (whitespace, quotes, ; $ ` < > | & ( ) or a backslash). The inventory "
                    f"expands the pattern on the machine, so only plain globs are accepted — "
                    f"[A-Za-z0-9_./*?[]@%+,:=~-]")
        for pattern in self.run_patterns:
            if pattern not in self.pull:
                raise ConfigError(
                    f"artefacts.run_patterns names {pattern!r}, which is not in artefacts.pull: a "
                    f"declaration that nothing is pulled for would be a silent no-op. Add it to pull, "
                    f"or drop it from run_patterns.")


@dataclass
class Config:
    provider: str = "runpod"
    budget: Budget = field(default_factory=Budget)
    artefacts: Artefacts = field(default_factory=Artefacts)
    runpod: dict = field(default_factory=dict)
    ssh_binary: str = "ssh"
    ssh_key: str = ""  # empty means: the provider's own key material, if it has any
    state_dir: Path = Path(".rentguard")
    #: Whether this config may **start** a rental at all. `up`, `adopt` and the MCP tools that call
    #: them refuse when it is false, before a provider is constructed or a row is opened, and `--force`
    #: does not override it. Everything that does not spend still works — `doctor`, `ledger`, `status`
    #: and above all `down`, because a live machine must be stoppable whatever its config says.
    #:
    #: It exists because a config is found by looking at the **current directory**: the MCP tools read
    #: `./rentguard.toml`, so a directory that happens to carry one decides the rules for everything
    #: started in it. Measured 2026-10-20: this workspace's root carried a decoy (COMMUNITY / 3080 Ti,
    #: a $0.10 cap, a ledger nobody read), and a rental through that door would have spent money under
    #: rules its operator never read. A decoy that can be deleted loses the ledger beside it; a decoy
    #: that can declare itself non-renting keeps its record and refuses out loud.
    allow_renting: bool = True
    #: Why this config refuses to rent — printed in the refusal and repeated by `doctor`. Optional, so a
    #: config written before this key existed keeps loading; empty means "no reason given", which the
    #: refusal says rather than inventing one.
    renting_disabled_reason: str = ""
    source: Path | None = None

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "runs.jsonl"

    @property
    def jobs_path(self) -> Path:
        return self.state_dir / "jobs.jsonl"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat.json"


def find_config(root: Path | None = None) -> Path | None:
    root = Path(root or Path.cwd())
    for candidate in (root / "rentguard.toml", Path.home() / ".config" / "rentguard" / "config.toml"):
        if candidate.is_file():
            return candidate
    return None


def load(root: Path | None = None, path: Path | None = None) -> Config:
    # $RENTGUARD_ROOT exists so that a runner started by something other than a shell in the
    # project directory — an MCP client, a service, a Bot — still reads the same config and
    # writes the same ledger. Two ledgers for one project is how spend goes missing.
    root = Path(root or os.environ.get("RENTGUARD_ROOT") or Path.cwd())
    if path is None:
        path = find_config(root)
    raw: dict = {}
    if path is not None:
        with open(path, "rb") as fh:
            try:
                raw = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    cfg = Config(provider=raw.get("provider", "runpod"), source=path)

    if isinstance(raw.get("budget"), dict):
        cfg.budget = Budget(**_only(raw["budget"], Budget))
    if isinstance(raw.get("artefacts"), dict):
        cfg.artefacts = Artefacts(**_only(raw["artefacts"], Artefacts))
    if isinstance(raw.get("runpod"), dict):
        cfg.runpod = raw["runpod"]
    if isinstance(raw.get("state_dir"), str):
        cfg.state_dir = Path(raw["state_dir"])
    if isinstance(raw.get("ssh_binary"), str):
        cfg.ssh_binary = raw["ssh_binary"]
    if isinstance(raw.get("ssh_key"), str):
        cfg.ssh_key = raw["ssh_key"]
    # The renting lock is read from the **file**, not defaulted into existence: a config that has never
    # heard of it rents exactly as before, and a config that sets it wrongly is refused at load rather
    # than read as `true` (a "no" that a typo turns into yes is the direction that spends money).
    if "allow_renting" in raw:
        allowed = raw["allow_renting"]
        if not isinstance(allowed, bool):
            raise ConfigError(f"allow_renting must be true or false, not {allowed!r}")
        cfg.allow_renting = allowed
    if "renting_disabled_reason" in raw:
        reason = raw["renting_disabled_reason"]
        if not isinstance(reason, str):
            raise ConfigError(f"renting_disabled_reason must be a string, not {reason!r}")
        cfg.renting_disabled_reason = reason.strip()

    # An absolute state dir is honoured; a relative one is relative to the project root.
    if not cfg.state_dir.is_absolute():
        cfg.state_dir = root / cfg.state_dir
    if cfg.source is not None and not Path(cfg.artefacts.local_dir).is_absolute():
        cfg.artefacts.local_dir = str((root / cfg.artefacts.local_dir).resolve())
    return cfg


def _only(section: dict, cls) -> dict:
    """Keep the keys the dataclass actually declares, so a typo cannot raise a
    TypeError deep inside the guard loop.

    The removed ``on_run`` switch is dropped rather than rejected: pulling as each run finishes and
    again on ``down`` is now unconditional, so a config that still carries the old key keeps working
    instead of failing at startup over a setting that no longer means anything.
    """
    allowed = set(getattr(cls, "__dataclass_fields__", {}))
    section = {k: v for k, v in section.items() if k != "on_run"}
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in [{cls.__name__.lower()}]: {sorted(unknown)}")
    return {k: v for k, v in section.items() if k in allowed}


def api_key(env_names: tuple[str, ...] = ("DSH_SECRET_RUNPOD_API_KEY", "RUNPOD_API_KEY")) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None
