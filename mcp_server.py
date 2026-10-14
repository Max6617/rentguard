#!/usr/bin/env python3
"""rentguard as an MCP stdio server, so the Bot team can rent and stop machines through it.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, per the MCP stdio convention. Nothing
else may be written to stdout — every command's output is captured and returned in the tool result,
because a stray print corrupts the stream and the client sees a dead server rather than an error.

Each tool call runs its command with ``--json`` and returns **both** forms: ``content`` is the text
a person would read, and ``structuredContent`` is the same answer as an object, so a Bot never has
to parse prose to find a cost or an instance id.

Environment:
    RENTGUARD_ROOT             project root: where rentguard.toml and .rentguard/ live
    DSH_SECRET_RUNPOD_API_KEY  the provider key (never printed, never returned)

Design rule: the money-moving tools are described as requiring the Main Bot's approval. The hard
cap in the config is the backstop for when a Bot ignores that — a policy that depends on everyone
remembering it is not a policy.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rentguard import __version__, cli  # noqa: E402

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = PROTOCOLS[0]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "rentguard_status",
        "description": (
            "Current rented instance: status, GPU, uptime, GPU utilisation, what this run has cost, "
            "total recorded spend against the cap, and whether the guard heartbeat is fresh. "
            "Read-only and always safe to call, including when nothing is rented."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rentguard_ledger",
        "description": "Every rental with its wall-clock and cost, and the total against the cap.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rentguard_doctor",
        "description": (
            "Check the configuration, the API key, recorded spend, the guard heartbeat, and — "
            "importantly — any live instance on the account that has NO ledger row. Run this "
            "before renting and whenever spend looks wrong."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rentguard_start",
        "description": (
            "Rent (or resume) a GPU instance. SPENDS MONEY: requires the Main Bot's approval "
            "unless the team has agreed otherwise. Refuses when recorded spend already exceeds "
            "the cap. The config's cap is enforced independently of this tool."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "description": "what this rental is for; goes in the ledger"},
                "gpu": {"type": "string", "description": "exact GPU id; omit to use the preference list"},
            },
            "required": ["purpose"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rentguard_run",
        "description": (
            "Run a shell command on the rented instance and pull the configured artefacts back when "
            "it finishes. Returns the command's exit code. Requires an instance that is genuinely "
            "usable: a machine reporting RUNNING with zero uptime and null ports is a crash loop "
            "and is refused."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "the command line to run remotely"},
                "workdir": {"type": "string", "description": "remote working directory"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rentguard_pull",
        "description": "Pull the artefacts configured in [artefacts].pull from the instance now.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rentguard_stop",
        "description": (
            "Stop the instance and close the ledger row with its real cost. Stopping is the default; "
            "leaving a machine running costs money per hour whether or not it is working."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rentguard_adopt",
        "description": (
            "Record an instance that is already running but has no ledger row, backdating the start "
            "time from the provider's uptime so the money already spent is recorded rather than "
            "hidden. Use when a live instance appears with no row."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string"},
                "purpose": {"type": "string"},
                "uptime_hours": {"type": "number",
                                 "description": "only when the instance is already stopped and the "
                                                "provider no longer reports uptime"},
            },
            "required": ["instance_id"],
            "additionalProperties": False,
        },
    },
]


def _call(func, *, json_mode: bool = True, **kwargs) -> tuple[str, dict | None, int | None]:
    """Run a CLI command and capture its output, so the JSON-RPC stream stays clean.

    With ``json_mode`` the command is asked for its structured document. Then stdout holds exactly
    one JSON object (the CLI sends prose to stderr in that mode), and both are captured separately:
    the caller gets the text a person would read *and* the object a Bot can act on. That is the
    "return the structured data alongside its text" rule, and it is why the two are captured rather
    than one being derived from the other.
    """
    args = argparse.Namespace(json=json_mode, **kwargs)
    out, err = io.StringIO(), io.StringIO()
    err_sink = contextlib.redirect_stderr(err) if json_mode else contextlib.nullcontext()
    code: int | None = 0
    with contextlib.redirect_stdout(out), err_sink:
        try:
            code = func(args)
        except SystemExit as exc:  # cli raises SystemExit for user errors
            code = exc.code if isinstance(exc.code, int) else 1
    stdout, stderr = out.getvalue().strip(), err.getvalue().strip()
    structured: dict | None = None
    if json_mode:
        try:
            parsed = json.loads(stdout)
            structured = parsed if isinstance(parsed, dict) else None
        except ValueError:
            structured = None
    text = stderr or stdout
    return text, structured, code


def dispatch(name: str, arguments: dict) -> tuple[str, dict | None, bool]:
    """``(text, structured, is_error)`` for one tool call."""
    arguments = arguments or {}
    if name == "rentguard_status":
        text, structured, code = _call(cli.cmd_status)
    elif name == "rentguard_ledger":
        text, structured, code = _call(cli.cmd_ledger)
    elif name == "rentguard_doctor":
        text, structured, code = _call(cli.cmd_doctor)
    elif name == "rentguard_start":
        text, structured, code = _call(cli.cmd_up, purpose=arguments.get("purpose"),
                                       gpu=arguments.get("gpu"), cap=None, per_run_cap=None,
                                       force=False)
    elif name == "rentguard_run":
        command = arguments.get("command") or ""
        if not command.strip():
            return "error: command is required", None, True
        text, structured, code = _call(cli.cmd_run, command=command.split(),
                                       workdir=arguments.get("workdir"))
    elif name == "rentguard_pull":
        text, structured, code = _call(cli.cmd_pull)
    elif name == "rentguard_stop":
        text, structured, code = _call(cli.cmd_down)
    elif name == "rentguard_adopt":
        text, structured, code = _call(cli.cmd_adopt, instance_id=arguments.get("instance_id"),
                                       purpose=arguments.get("purpose"),
                                       uptime_hours=arguments.get("uptime_hours"))
    else:
        return f"error: unknown tool {name}", None, True
    if code not in (0, None):
        text = f"{text}\n(exit code {code})".strip()
    return text, structured, code not in (0, None)


def _respond(msg_id, result=None, error=None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        version = requested if requested in PROTOCOLS else LATEST
        _respond(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rentguard", "version": __version__},
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return  # notifications carry no id and get no reply

    if method == "ping":
        _respond(msg_id, {})
        return

    if method == "tools/list":
        _respond(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name") or ""
        text, structured, is_error = dispatch(name, params.get("arguments") or {})
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": text or "(no output)"}],
            "isError": is_error,
        }
        if structured is not None:
            # The same answer, as data. A Bot should not have to parse prose to find the cost.
            result["structuredContent"] = structured
        _respond(msg_id, result)
        return

    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue  # a malformed line must not kill the server
        if not isinstance(message, dict):
            # Valid JSON, but not a request object: a list, a bare string, a number. It has no id and
            # no method, so there is nothing to answer and nobody to answer to. Skipping it is the
            # only safe move — calling .get() on it is how a server dies on a stray line.
            continue
        try:
            handle(message)
        except Exception as exc:  # noqa: BLE001 - a crash here looks like a dead server
            # The handler itself failed. Answer with an error rather than exiting, or the client
            # sees a dead server instead of a failed call.
            if message.get("id") is not None:
                _respond(message["id"], error={"code": -32603,
                                               "message": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
