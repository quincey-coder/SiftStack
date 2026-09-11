"""Tests for the skip-trace spend hooks. Offline, no spend.

  .claude/hooks/skip_spend_approve.py  (UserPromptSubmit: owner's "yes" -> token)
  .claude/hooks/skip_spend_gate.py     (PreToolUse: blocks forging the token)

Feeds synthetic hook payloads to the real hook processes. Any live pending /
token files are saved first and restored afterwards.

    python tests/test_skip_spend_gate.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
STATE = ROOT / "data" / "skip_orchestrator"
PENDING = STATE / "pending_approval.json"
TOKEN = STATE / "approval_token.json"


def _hook(name, payload):
    p = subprocess.run([sys.executable, str(HOOKS / name)],
                       input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr.decode()
    out = p.stdout.decode().strip()
    return json.loads(out) if out else None


def _guard(tool, **tool_input):
    out = _hook("skip_spend_gate.py", {"hook_event_name": "PreToolUse", "tool_name": tool,
                                       "tool_input": tool_input, "permission_mode": "auto"})
    return out["hookSpecificOutput"]["permissionDecision"] if out else None


def _pending(max_cost=5.0, eligible=2, age=0):
    STATE.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({
        "input": "c:\\x\\export.csv", "file": "export.csv", "max_cost": max_cost,
        "eligible": eligible, "total_low": 0.68, "total_high": 0.92,
        "smartskip": True, "directskip": True, "created": time.time() - age}))


def _say(prompt):
    return _hook("skip_spend_approve.py", {"hook_event_name": "UserPromptSubmit", "prompt": prompt})


def _clear():
    for f in (PENDING, TOKEN):
        f.unlink(missing_ok=True)


# ── owner's-message hook ──────────────────────────────────────────────

def test_plain_yes_approves_pending_into_a_token():
    for reply in ("yes", "Yes, it's good to go", "approve", "ok go ahead"):
        _clear(); _pending()
        out = _say(reply)
        assert "APPROVED" in out["systemMessage"], reply
        tok = json.loads(TOKEN.read_text())
        assert tok["max_cost"] == 5.0 and tok["eligible"] == 2, reply
        assert not PENDING.exists(), reply


def test_yes_with_amount_sets_the_ceiling():
    _clear(); _pending()
    _say("yes, cap $12")
    assert json.loads(TOKEN.read_text())["max_cost"] == 12.0


def test_amount_below_skip_cost_is_refused():
    _clear(); _pending(eligible=100)          # skip cost 100 x 0.25 = $25
    out = _say("yes $5")
    assert "NOT approved" in out["systemMessage"] and not TOKEN.exists()


def test_conditional_yes_does_not_approve():
    for reply in ("yes but only the first 10", "yes, wait", "ok but not the LLCs"):
        _clear(); _pending()
        out = _say(reply)
        assert "NOT approved" in out["systemMessage"] and not TOKEN.exists(), reply
        assert PENDING.exists(), reply        # still pending


def test_no_cancels_the_pending():
    _clear(); _pending()
    out = _say("no, hold off")
    assert "CANCELLED" in out["systemMessage"]
    assert not PENDING.exists() and not TOKEN.exists()


def test_silent_without_a_pending_or_for_unrelated_messages():
    _clear()
    assert _say("yes") is None and not TOKEN.exists()
    _pending()
    assert _say("can you explain how the merge step works in more detail please") is None
    assert not TOKEN.exists() and PENDING.exists()


def test_expired_pending_is_not_approved():
    _clear(); _pending(age=4000)
    out = _say("yes")
    assert "NOT approved" in out["systemMessage"] and not TOKEN.exists()


# ── PreToolUse guard ──────────────────────────────────────────────────

def test_guard_blocks_forging_the_token_from_the_shell():
    for cmd in ('echo {} > data\\skip_orchestrator\\approval_token.json',
                'Set-Content data/skip_orchestrator/pending_approval.json "{}"',
                'python -c "import skip_orchestrator as so; so.approve_pending()"',
                'python -c "from skip_orchestrator import write_approval_token"'):
        assert _guard("PowerShell", command=cmd) == "deny", cmd
        assert _guard("Bash", command=cmd) == "deny", cmd


def test_guard_blocks_writing_the_state_files():
    for tool in ("Write", "Edit", "MultiEdit"):
        assert _guard(tool, file_path=str(TOKEN), content="{}") == "deny", tool
        assert _guard(tool, file_path=str(PENDING), content="{}") == "deny", tool


def test_guard_allows_the_normal_cli_and_unrelated_work():
    for cmd in ('python src\\skip_orchestrator.py estimate --input "C:\\x\\e.csv"',
                'python src\\skip_orchestrator.py run --input "C:\\x\\e.csv" --max-cost 5',
                'python tests\\test_skip_orchestrator.py',
                'Get-Content data\\skip_orchestrator\\gate_log.jsonl',
                'git status'):
        assert _guard("PowerShell", command=cmd) is None, cmd
    assert _guard("Edit", file_path=str(ROOT / "src" / "skip_orchestrator.py")) is None


def _run():
    saved = {f: f.read_bytes() for f in (PENDING, TOKEN) if f.exists()}
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"  [PASS] {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    finally:
        _clear()
        for f, b in saved.items():
            f.write_bytes(b)
    print(f"\n  {passed}/{len(tests)} tests passed.")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
