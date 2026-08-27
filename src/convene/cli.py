"""The ``convene`` command line.

Subcommands:

    ask       one-shot text completion
    json      one-shot completion validated against a JSON Schema
    experts   list and lint an expert registry
    run       run one expert over a JSONL file, resumably
    chat      an interactive live session in your terminal
    doctor    check setup, and which account is about to be billed
    bench     reproduce the measurements in FINDINGS.md
    setup-token  mint a long-lived token for unattended use
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import __version__
from .auth import DEFAULT_AUTH, AuthMode, ensure_dirs
from .config import (
    CHEAP_MODEL,
    DEFAULT_CONCURRENCY,
    DEFAULT_MODEL,
    SANDBOX_TOKEN_FILE,
)
from .doctor import run_checks, worst
from .errors import ConveneError
from .experts import Registry, estimate_tokens, map_expert
from .runtime import Call, run, run_sync
from .sessions import LiveSession


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _load_registry(path: str | None) -> Registry:
    """Find an expert registry, by flag, environment, or convention."""
    candidates = [
        path,
        os.environ.get("CONVENE_EXPERTS"),
        "experts.toml",
        "convene.toml",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return Registry.load(candidate)
    raise ConveneError(
        "no expert registry found. Pass --experts PATH, set CONVENE_EXPERTS, "
        "or create experts.toml in the working directory."
    )


# ---- ask / json ------------------------------------------------------------
def cmd_ask(args: argparse.Namespace) -> int:
    prompt = args.prompt or sys.stdin.read()
    call = Call(
        user_prompt=prompt,
        system_prompt=args.system,
        model=args.model,
        effort=args.effort,
        max_budget_usd=args.max_budget_usd,
        log_tag="cli-ask",
    )
    result = run_sync(call, auth_mode=AuthMode(args.auth))
    print(result.text or "")
    if args.usage:
        _eprint(
            f"[{result.model} {result.elapsed_s:.1f}s in={result.input_tokens} "
            f"out={result.output_tokens} cache_read={result.cache_read_input_tokens} "
            f"cost=${result.cost_usd:.5f}]"
        )
    return 0


def cmd_json(args: argparse.Namespace) -> int:
    schema_text = (
        Path(args.schema).read_text(encoding="utf-8")
        if Path(args.schema).exists()
        else args.schema
    )
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as e:
        raise ConveneError(
            f"--schema is neither a readable file nor valid JSON: {e}"
        ) from e

    prompt = args.prompt or sys.stdin.read()
    call = Call(
        user_prompt=prompt,
        system_prompt=args.system,
        model=args.model,
        effort=args.effort,
        json_schema=schema,
        max_budget_usd=args.max_budget_usd,
        log_tag="cli-json",
    )
    result = run_sync(call, auth_mode=AuthMode(args.auth))
    print(json.dumps(result.structured_output, indent=2))
    return 0


# ---- experts ---------------------------------------------------------------
def cmd_experts(args: argparse.Namespace) -> int:
    registry = _load_registry(args.experts)

    if args.action == "lint":
        problems = registry.lint()
        if not problems:
            print(f"{len(registry)} expert(s) in {registry.source}: no problems found.")
            return 0
        for problem in problems:
            print(f"warn: {problem}")
        print(f"\n{len(problems)} warning(s).")
        return 1

    print(f"{len(registry)} expert(s) in {registry.source}\n")
    width = max((len(e.name) for e in registry), default=4)
    for expert in sorted(registry, key=lambda e: e.name):
        tokens = estimate_tokens(expert.system_prompt)
        cache = "cached" if expert.cacheable else "TOO SHORT TO CACHE"
        print(
            f"  {expert.name:<{width}}  {expert.model}"
            f"{'/' + expert.effort if expert.effort else ''}"
            f"  ~{tokens} sys tokens ({cache})"
        )
        if expert.description:
            print(f"  {'':<{width}}  {expert.description}")
    return 0


# ---- run (batch) -----------------------------------------------------------
def _read_items(path: str, field: str) -> list[dict[str, Any]]:
    """Read JSONL (or plain lines) into {id, prompt} records."""
    # Not a `with`: the handle is either stdin (which must not be closed) or a
    # file, and the try/finally below closes only the latter.
    handle = sys.stdin if path == "-" else open(path, encoding="utf-8")  # noqa: SIM115
    items: list[dict[str, Any]] = []
    try:
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A plain text file is a legitimate input too.
                items.append({"id": str(index), "prompt": line})
                continue
            if isinstance(record, str):
                items.append({"id": str(index), "prompt": record})
            elif isinstance(record, dict):
                if field not in record:
                    raise ConveneError(
                        f"line {index + 1} has no {field!r} field. "
                        f"Use --field to name the prompt field."
                    )
                items.append(
                    {
                        "id": str(record.get("id", index)),
                        "prompt": str(record[field]),
                        "meta": record,
                    }
                )
            else:
                raise ConveneError(f"line {index + 1} is neither an object nor a string")
    finally:
        if handle is not sys.stdin:
            handle.close()
    return items


def _already_done(out_path: str) -> set[str]:
    """Ids already present in the output file, so a rerun resumes."""
    path = Path(out_path)
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(str(json.loads(line).get("id")))
        except json.JSONDecodeError:
            continue
    return done


def cmd_run(args: argparse.Namespace) -> int:
    registry = _load_registry(args.experts)
    expert = registry.get(args.expert)
    items = _read_items(args.input, args.field)

    done = _already_done(args.output) if args.output and args.resume else set()
    pending = [i for i in items if i["id"] not in done]
    if done:
        _eprint(f"resuming: {len(done)} already done, {len(pending)} to go")
    if not pending:
        _eprint("nothing to do")
        return 0

    if not expert.cacheable:
        _eprint(
            f"warn: {expert.name}'s system prompt is ~"
            f"{estimate_tokens(expert.system_prompt)} tokens, too short for the "
            f"prompt cache. Every call will pay full price. `convene experts lint`"
        )

    start = time.monotonic()
    results = asyncio.run(
        map_expert(
            expert.name,
            [i["prompt"] for i in pending],
            registry=registry,
            auth_mode=AuthMode(args.auth),
            concurrency=args.concurrency,
        )
    )
    elapsed = time.monotonic() - start

    # Same reason as _read_items: stdout must survive, an opened file must not.
    out = (
        open(args.output, "a", encoding="utf-8")  # noqa: SIM115
        if args.output
        else sys.stdout
    )
    failures = 0
    cost = 0.0
    cache_hits = 0
    try:
        for item, result in zip(pending, results, strict=True):
            if isinstance(result, Exception):
                failures += 1
                record = {"id": item["id"], "error": str(result)}
            else:
                cost += result.cost_usd
                cache_hits += 1 if result.cache_hit else 0
                record = {
                    "id": item["id"],
                    "output": result.structured_output
                    if result.structured_output is not None
                    else result.text,
                    "usage": {
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cache_read_input_tokens": result.cache_read_input_tokens,
                        "cost_usd": result.cost_usd,
                        "elapsed_s": round(result.elapsed_s, 2),
                    },
                }
            out.write(json.dumps(record) + "\n")
            out.flush()
    finally:
        if out is not sys.stdout:
            out.close()

    _eprint(
        f"\n{len(pending) - failures}/{len(pending)} ok, {failures} failed | "
        f"{elapsed:.1f}s wall | ${cost:.4f} | "
        f"{cache_hits}/{len(pending)} warm cache reads"
    )
    return 1 if failures else 0


# ---- chat ------------------------------------------------------------------
def cmd_chat(args: argparse.Namespace) -> int:
    system = args.system
    if args.experts or args.expert:
        registry = _load_registry(args.experts)
        if args.expert:
            expert = registry.get(args.expert)
            system = expert.system_prompt
            args.model = expert.model
            args.effort = expert.effort

    print(
        f"convene chat -- {args.model}"
        f"{'/' + args.effort if args.effort else ''}. "
        f"Ctrl-D or /quit to exit.\n"
    )
    with LiveSession(
        system_prompt=system,
        model=args.model,
        effort=args.effort,
        auth_mode=AuthMode(args.auth),
    ) as session:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            try:
                turn = session.ask(line)
            except ConveneError as e:
                _eprint(f"error: {e}")
                break
            print(f"\n{turn.text}\n")
            if args.usage:
                _eprint(
                    f"[{turn.elapsed_s:.1f}s in={turn.input_tokens} "
                    f"out={turn.output_tokens} +${turn.cost_usd:.5f}]"
                )
        print(f"{len(session.turns)} turn(s), ${session.total_cost_usd:.4f} total.")
    return 0


# ---- doctor ----------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"convene {__version__}\n")
    checks = run_checks(
        auth_mode=AuthMode(args.auth),
        expected_email=args.expect_email,
        probe=not args.no_probe,
        lockdown=args.lockdown,
    )
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"  [{check.icon}] {check.name:<{width}}  {check.detail}")
        if check.fix:
            print(f"        {'':<{width}}  fix: {check.fix}")
    overall = worst(checks)
    print()
    print(
        {
            "ok": "All checks passed.",
            "warn": "Usable, with warnings.",
            "fail": "Not usable.",
        }[overall]
    )
    return {"ok": 0, "warn": 0, "fail": 1}[overall]


# ---- bench -----------------------------------------------------------------
def cmd_bench(args: argparse.Namespace) -> int:
    """Reproduce the measurements the documentation claims.

    Every empirical claim in this repo should be re-runnable by whoever doubts
    it. This is that command.
    """
    from .auth import cli_version

    model = args.model
    print(f"convene bench -- Claude Code {cli_version()}, model {model}\n")
    total = 0.0

    if "cache" in args.suite:
        print("== prompt cache: same long system prompt, three separate processes ==")
        big = "You are a document classifier for an enterprise archive. " * 260
        print(f"   system prompt ~{estimate_tokens(big)} tokens\n")
        for i in range(1, 4):
            r = run_sync(
                Call(
                    user_prompt=f"Classify: invoice #{i}. One word.",
                    system_prompt=big,
                    model=model,
                    effort="low",
                    log_tag="bench-cache",
                ),
                auth_mode=AuthMode(args.auth),
            )
            total += r.cost_usd
            label = "cold" if i == 1 else "warm"
            print(
                f"   {label:<5} cache_create={r.cache_creation_input_tokens:>5} "
                f"cache_read={r.cache_read_input_tokens:>5} "
                f"cost=${r.cost_usd:.5f}  ({r.elapsed_s:.1f}s)"
            )
        print()

    if "concurrency" in args.suite:
        print("== concurrency: identical trivial calls, one burst each ==")

        async def burst(n: int) -> tuple[float, list[float], int]:
            start = time.monotonic()
            calls = [
                Call(
                    user_prompt=f"Say the number {i}. Digits only.",
                    system_prompt="You are terse.",
                    model=model,
                    effort="low",
                    log_tag="bench-conc",
                )
                for i in range(n)
            ]
            outcomes = await asyncio.gather(
                *(run(c, auth_mode=AuthMode(args.auth)) for c in calls),
                return_exceptions=True,
            )
            wall = time.monotonic() - start
            oks = [o for o in outcomes if not isinstance(o, BaseException)]
            return wall, sorted(o.elapsed_s for o in oks), len(outcomes) - len(oks)

        for n in args.sizes:
            wall, lat, errs = asyncio.run(burst(n))
            med = lat[len(lat) // 2] if lat else float("nan")
            print(
                f"   n={n:<3} wall={wall:5.1f}s  per-call med={med:5.1f}s  errors={errs}"
            )
        print()

    print(
        f"Estimated spend for this run: ${total:.4f} (cache suite only; "
        f"list-price estimate, not a charge)."
    )
    print("Please report results in an issue with your Claude Code version.")
    return 0


# ---- setup-token -----------------------------------------------------------
def cmd_setup_token(args: argparse.Namespace) -> int:
    """Mint a long-lived subscription token for unattended runs."""
    import getpass

    ensure_dirs()
    if SANDBOX_TOKEN_FILE.exists() and SANDBOX_TOKEN_FILE.stat().st_size > 0:
        print(f"A token is already present at {SANDBOX_TOKEN_FILE}")
        print("Delete it and re-run to mint a new one.")
        return 0

    print("Running `claude setup-token` -- follow the browser prompt.")
    print("It mints a long-lived token tied to your Claude subscription, so")
    print("unattended runs draw on the subscription rather than API credits.\n")
    subprocess.run(["claude", "setup-token"], check=False)
    print()
    token = getpass.getpass("Paste the token (input hidden): ").strip()
    if not token:
        _eprint("No token entered; nothing written.")
        return 1

    SANDBOX_TOKEN_FILE.write_text(token, encoding="utf-8")
    SANDBOX_TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Wrote {SANDBOX_TOKEN_FILE} (mode 600).")
    print("\nVerify with:  convene doctor --auth sandbox_token")
    return 0


# ---- parser ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convene",
        description="Run Claude Code headlessly as a local inference layer.",
    )
    parser.add_argument("--version", action="version", version=f"convene {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument(
            "--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None
        )
        p.add_argument(
            "--auth",
            choices=[m.value for m in AuthMode],
            default=DEFAULT_AUTH.value,
            help=(
                "login (default, subscription) | sandbox_token | "
                "api_key (bills API credits)"
            ),
        )

    p_ask = sub.add_parser("ask", help="one-shot text completion")
    p_ask.add_argument("prompt", nargs="?", help="prompt, or read stdin if omitted")
    p_ask.add_argument("--system", default="You are a concise, accurate assistant.")
    p_ask.add_argument("--max-budget-usd", type=float, default=None)
    p_ask.add_argument("--usage", action="store_true", help="print usage to stderr")
    add_common(p_ask)
    p_ask.set_defaults(func=cmd_ask)

    p_json = sub.add_parser("json", help="completion validated against a JSON Schema")
    p_json.add_argument("prompt", nargs="?")
    p_json.add_argument(
        "--schema", required=True, help="path to a schema file, or inline JSON"
    )
    p_json.add_argument(
        "--system", default="You extract structured data. Follow the schema exactly."
    )
    p_json.add_argument("--max-budget-usd", type=float, default=None)
    add_common(p_json)
    p_json.set_defaults(func=cmd_json)

    p_exp = sub.add_parser("experts", help="list or lint an expert registry")
    p_exp.add_argument("action", choices=["list", "lint"], nargs="?", default="list")
    p_exp.add_argument("--experts", default=None, help="path to a registry TOML")
    p_exp.set_defaults(func=cmd_experts)

    p_run = sub.add_parser("run", help="run one expert over a JSONL file")
    p_run.add_argument("--expert", required=True)
    p_run.add_argument(
        "--in", dest="input", required=True, help="JSONL file, or - for stdin"
    )
    p_run.add_argument("--out", dest="output", default=None, help="JSONL file (appended)")
    p_run.add_argument("--field", default="prompt", help="field holding the prompt")
    p_run.add_argument("--experts", default=None)
    p_run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_run.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="reprocess ids already present in --out",
    )
    p_run.add_argument(
        "--auth", choices=[m.value for m in AuthMode], default=DEFAULT_AUTH.value
    )
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="interactive live session")
    p_chat.add_argument("--system", default="You are a concise, accurate assistant.")
    p_chat.add_argument("--expert", default=None, help="use an expert's prompt and model")
    p_chat.add_argument("--experts", default=None)
    p_chat.add_argument("--usage", action="store_true")
    add_common(p_chat)
    p_chat.set_defaults(func=cmd_chat)

    p_doc = sub.add_parser("doctor", help="check setup and billing account")
    p_doc.add_argument(
        "--expect-email", default=None, help="fail if a different account is logged in"
    )
    p_doc.add_argument("--no-probe", action="store_true", help="skip the live test call")
    p_doc.add_argument(
        "--lockdown",
        action="store_true",
        help="also measure locked-down vs plain (costs a few cents)",
    )
    p_doc.add_argument(
        "--auth", choices=[m.value for m in AuthMode], default=DEFAULT_AUTH.value
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_bench = sub.add_parser("bench", help="reproduce the measurements in FINDINGS.md")
    p_bench.add_argument(
        "--suite",
        nargs="+",
        choices=["cache", "concurrency"],
        default=["cache", "concurrency"],
    )
    p_bench.add_argument("--sizes", nargs="+", type=int, default=[1, 5, 10, 20])
    p_bench.add_argument("--model", default=CHEAP_MODEL)
    p_bench.add_argument(
        "--auth", choices=[m.value for m in AuthMode], default=DEFAULT_AUTH.value
    )
    p_bench.set_defaults(func=cmd_bench)

    p_tok = sub.add_parser("setup-token", help="mint a long-lived subscription token")
    p_tok.set_defaults(func=cmd_setup_token)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except ConveneError as e:
        _eprint(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        _eprint("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
