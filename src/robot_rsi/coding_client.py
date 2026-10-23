"""Existing Codex exec transport and observed events; no delivery or Git policy."""
import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from .host_boundary import command

def client_arguments(arguments):
    parser = argparse.ArgumentParser(description="Existing Codex exec options inside the controlled coding entry")
    parser.add_argument("--model", "-m")
    parser.add_argument("--config", "-c", action="append", default=[])
    parser.add_argument("--sandbox", "-s")
    for flag in ("ignore-user-config", "ignore-rules", "dangerously-bypass-approvals-and-sandbox", "skip-git-repo-check", "json"):
        parser.add_argument("--" + flag, action="store_true")
    parser.add_argument("--output-last-message", "-o")
    parser.add_argument("prompt", nargs="?", default="-")
    args = parser.parse_args(arguments)
    options = []
    if args.model:
        options += ["--model", args.model]
    for setting in args.config + (["sandbox_mode=" + json.dumps(args.sandbox)] if args.sandbox else []):
        options += ["-c", setting]
    for flag in ("ignore-user-config", "ignore-rules", "dangerously-bypass-approvals-and-sandbox", "skip-git-repo-check"):
        if getattr(args, flag.replace("-", "_")):
            options.append("--" + flag)
    if args.output_last_message:
        options += ["--output-last-message", args.output_last_message]
    return options, sys.stdin.read() if args.prompt == "-" else args.prompt

def run_client(root, project, options, prompt, schema, record, *, reads=(), auth=None, network=False, session=None):
    arguments = (["resume"] if session else []) + options + ["--json", "--output-schema", str(schema)]
    if session:
        arguments.append(session)
    arguments.append("-")
    argv = command(root, project, "coding", arguments, reads=reads, auth=auth, network=network)
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    final, interrupted, terminal, usage = None, False, None, None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item", {})
            if item.get("type") == "reasoning":
                continue
            print(line, end="", flush=True)
            if event.get("type") in {"turn.completed", "turn.failed", "error"}:
                terminal, usage = event["type"], event.get("usage")
            if event.get("type") == "thread.started":
                identity = str(uuid.UUID(event["thread_id"]))
                if session and identity != session:
                    raise ValueError("Resume returned another session; do not continue automatically")
                record["session_id"] = identity
            if event.get("type") == "item.completed":
                if item.get("type") == "command_execution":
                    record["commands"].append({"id": item.get("id"), "command": item["command"], "exit_code": item.get("exit_code")})
                elif item.get("type") == "agent_message":
                    final = item.get("text")
                elif item.get("type") == "file_change":
                    record.setdefault("file_changes", []).extend(c["path"] for c in item.get("changes", []) if isinstance(c.get("path"), str))
        process.wait()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    record["turns"].append({"session_id": record.get("session_id"), "resume": bool(session),
                             "exit_code": process.returncode, "interrupted": interrupted,
                             "terminal": terminal, "usage": usage})
    return final, interrupted, process.returncode or (0 if terminal == "turn.completed" else 1)
