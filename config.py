"""Configuration for rentguard.

Search order for the project file:
    ./rentguard.toml
    ~/.config/rentguard/config.toml

The budget section is the point of the tool, so it is parsed into a dataclass and validated
eagerly: a negative cap or an idle window of zero is a mistake worth failing on at startup,
not at 3am when the guard is deciding whether to stop a machine.
"""

from __future__ import annotations

import os
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


@dataclass
class Artefacts:
    pull: list[str] = field(default_factory=list)
    local_dir: str = "./artefacts"
    remote_dir: str = "/workspace"
    #: There is deliberately no switch here. If ``pull`` is non-empty, `run` pulls when each run
    #: finishes and `down` pulls before it stops. The old ``on_run`` setting selected between those
    #: and doing nothing; it is deleted rather than defaulted, because a tool whose promise is "no
    #: surprises" cannot carry a setting that turns the promise off. A config that still has the key
    #: is accepted and the key ignored — see ``_only``.


@dataclass
class Config:
    provider: str = "runpod"
    budget: Budget = field(default_factory=Budget)
    artefacts: Artefacts = field(default_factory=Artefacts)
    runpod: dict = field(default_factory=dict)
    ssh_binary: str = "ssh"
    ssh_key: str = ""  # empty means: the provider's own key material, if it has any
    state_dir: Path = Path(".rentguard")
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


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


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
