"""Run one non-interactive `claude -p` call.

Copied from deal-terms-desk pipeline/claude.py (MIT, same author).
"""
import json
import subprocess
import tempfile


def run_claude(prompt: str, model: str, timeout: int = 900, schema: dict | None = None,
               effort: str | None = None) -> dict:
    """One non-interactive `claude -p` call on the Max plan; $0 cash.

    No tools and no settings, project or user (so no CLAUDE.md, hooks or MCP servers), and run from a fresh
    empty directory outside the repo, so the model sees only the prompt. Every failure raises RuntimeError
    with a plain message.

    `schema` adds `--json-schema <compact JSON>` (the reply then carries a `structured_output` dict beside
    `result`) and `effort` adds `--effort <level>`, each only when given, so M1's calls are unchanged.
    """
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--setting-sources", ""]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, sort_keys=True, separators=(",", ":"))]
    if effort:
        cmd += ["--effort", effort]
    try:
        with tempfile.TemporaryDirectory(prefix="askmike-claude-") as cwd:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, check=True,
                                  cwd=cwd)
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"claude exited {e.returncode}: {(e.stderr or '')[:500]}") from None
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"claude returned non-JSON output: {done.stdout[:200]!r}") from None
    if not isinstance(reply, dict) or reply.get("is_error") or "result" not in reply:
        raise RuntimeError(f"claude returned an error reply: {str(reply)[:500]}")
    return reply
