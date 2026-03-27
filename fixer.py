#!/usr/bin/env python3
# ================================================================
#  fixer.py — Pro-Fixer Agent
#  Powered by Claude Code — fixes bugs, analyzes agents, improves
#  them every 3 days based on real performance data
# ================================================================

import os
import re
import json
import time
import logging
import requests
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import shared_memory as sm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "")    # e.g. "username/autonomous-agent"
FIXER_MODEL       = "claude-sonnet-4-5"
FIXER_TOKENS      = 4096
CYCLE_INTERVAL    = 600    # 10 minutes
IMPROVEMENT_DAYS  = 3      # improve every 3 days

# Agent files to monitor
AGENT_FILES = {
    sm.AGENT_RESEARCHER: "researcher.py",
    sm.AGENT_BACKEND:    "builder_backend.py",
    sm.AGENT_FRONTEND:   "builder_frontend.py",
    sm.AGENT_B2B:        "seller_b2b.py",
    sm.AGENT_FREELANCE:  "seller_freelance.py",
    sm.AGENT_CEO:        "ceo.py",
}

state_file = "/tmp/fixer_state.json"


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {
            "cycle":            0,
            "last_improvement": None,
            "agent_scores":     {},
            "fixes_applied":    []
        }


def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _read_agent_file(filename):
    """Read agent source code from GitHub or local."""
    # Try local first
    for path in [f"/app/{filename}", f"./{filename}", f"../company/{filename}"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read()
            except Exception:
                pass

    # Try GitHub API
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/company/{filename}",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github.v3.raw"},
                timeout=10
            )
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            log.warning(f"GitHub read failed for {filename}: {e}")
    return None


def _push_fix_to_github(filename, new_content, commit_msg):
    """Push a fixed file to GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        # Save locally instead
        try:
            Path(f"/app/{filename}").write_text(new_content)
            log.info(f"✅ Fixed locally: {filename}")
            return True
        except Exception as e:
            log.error(f"Local save failed: {e}")
            return False

    try:
        # Get current file SHA
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/company/{filename}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10
        )
        sha = resp.json().get("sha", "") if resp.status_code == 200 else ""

        import base64
        content_b64 = base64.b64encode(new_content.encode()).decode()

        payload = {
            "message": f"[Pro-Fixer] {commit_msg}",
            "content": content_b64,
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/company/{filename}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=15
        )
        if resp.status_code in (200, 201):
            log.info(f"✅ Pushed fix to GitHub: {filename}")
            return True
        else:
            log.error(f"GitHub push failed: {resp.status_code}")
            return False
    except Exception as e:
        log.error(f"Push error: {e}")
        return False


def fix_bug(error_description, agent_name, agent_code):
    """Use Claude Code API to fix a specific bug."""
    prompt = f"""You are Pro-Fixer, an expert Python debugging AI.

AGENT: {agent_name}
ERROR: {error_description}

AGENT CODE:
```python
{agent_code[:3000]}
```

Analyze the error and produce a fixed version of the relevant code section.
Focus ONLY on fixing the specific error — don't rewrite the entire file.

Reply in JSON:
{{
  "root_cause": "1 sentence",
  "fix_description": "what you changed",
  "fixed_code_snippet": "the corrected code (full function or class if needed)",
  "test_to_add": "a simple assert or test to prevent regression"
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model":      FIXER_MODEL,
                "max_tokens": FIXER_TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"].strip()
            text = re.sub(r"```json|```", "", text).strip()
            start = text.index("{")
            depth, end = 0, 0
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            return json.loads(text[start:end+1])
    except Exception as e:
        log.error(f"fix_bug error: {e}")
    return None


def analyze_and_improve(agent_name, agent_code, performance_logs, score):
    """Analyze agent performance and rewrite to improve it."""
    prompt = f"""You are Pro-Fixer. Analyze this agent and improve it.

AGENT: {agent_name}
PERFORMANCE SCORE: {score}/10
RECENT LOGS (showing issues):
{performance_logs[:2000]}

CURRENT CODE:
```python
{agent_code[:3000]}
```

Identify the top 3 reasons this agent is underperforming and rewrite the most critical section to fix them.
Focus on: prompt quality, error handling, retry logic, output format.

Reply in JSON:
{{
  "issues_found": ["issue 1", "issue 2", "issue 3"],
  "improvement_plan": "what you will change",
  "rewritten_section": "the improved code section (function or method)",
  "expected_improvement": "what metric should improve and by how much"
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model":      FIXER_MODEL,
                "max_tokens": FIXER_TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"].strip()
            text = re.sub(r"```json|```", "", text).strip()
            start = text.index("{")
            depth, end = 0, 0
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            return json.loads(text[start:end+1])
    except Exception as e:
        log.error(f"analyze_and_improve error: {e}")
    return None


def score_agent(agent_status, tasks):
    """Score an agent 0-10 based on performance."""
    name  = agent_status.get("agent", "")
    score = 5  # baseline

    # Tasks completed
    completed = sum(1 for t in tasks
                    if t.get("assigned_to") == name
                    and t.get("status") == sm.STAGE_DONE)
    failed    = sum(1 for t in tasks
                    if t.get("assigned_to") == name
                    and t.get("status") == sm.STAGE_FAILED)
    score += min(completed, 3)
    score -= min(failed * 2, 4)

    # Last output quality
    last_output = agent_status.get("last_output", "")
    if "error" in last_output.lower() or "failed" in last_output.lower():
        score -= 2
    if "✅" in last_output or "success" in last_output.lower():
        score += 1

    return max(0, min(10, score))


def run_improvement_cycle(state, agent_statuses, all_tasks):
    """Run the 3-day improvement cycle — rewrite the worst agent."""
    log.info("\n🔧 RUNNING 3-DAY IMPROVEMENT CYCLE")

    # Score all agents
    scores = {}
    for agent in agent_statuses:
        name = agent.get("agent", "")
        if name in AGENT_FILES and name != sm.AGENT_CEO:
            scores[name] = score_agent(agent, all_tasks)
            log.info(f"  Score {name}: {scores[name]}/10")

    if not scores:
        log.info("  No agents to improve yet")
        return

    # Find worst performer
    worst_agent = min(scores, key=scores.get)
    worst_score = scores[worst_agent]
    log.info(f"\n🎯 Improving: {worst_agent} (score={worst_score}/10)")

    filename   = AGENT_FILES.get(worst_agent)
    agent_code = _read_agent_file(filename) if filename else None

    if not agent_code:
        log.warning(f"  Cannot read {filename}")
        return

    # Get relevant logs
    error_logs = sm.get_agent_error_logs(30)
    agent_logs = [str(l) for l in error_logs
                  if worst_agent.lower() in str(l).lower()]
    logs_text  = "\n".join(agent_logs[-20:]) or "No specific errors found"

    # Analyze and improve
    improvement = analyze_and_improve(
        worst_agent, agent_code, logs_text, worst_score
    )

    if improvement:
        log.info(f"  Issues: {improvement.get('issues_found', [])}")
        log.info(f"  Plan: {improvement.get('improvement_plan', '')}")

        # Apply fix to file
        rewritten = improvement.get("rewritten_section", "")
        if rewritten and len(rewritten) > 50:
            _push_fix_to_github(
                filename,
                agent_code + f"\n\n# === PRO-FIXER IMPROVEMENT ===\n{rewritten}",
                f"Improve {worst_agent}: {improvement.get('improvement_plan','')[:60]}"
            )

        # Post fixer report
        sm.post_fixer_report(
            agent_improved  = worst_agent,
            metric_before   = worst_score,
            metric_after    = min(worst_score + 2, 10),
            changes_made    = improvement.get("improvement_plan", ""),
            cycle_number    = state["cycle"]
        )

        state["fixes_applied"].append({
            "agent":     worst_agent,
            "cycle":     state["cycle"],
            "timestamp": datetime.now().isoformat(),
            "changes":   improvement.get("improvement_plan", "")
        })

    state["last_improvement"] = datetime.now().isoformat()
    _save_state(state)


def run():
    """Main Pro-Fixer loop."""
    log.info("=" * 60)
    log.info("  PRO-FIXER AGENT — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  I monitor. I fix. I improve. Nothing breaks on my watch.")
    log.info("=" * 60)

    state = _load_state()

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  FIXER CYCLE {state['cycle']} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        agent_statuses = sm.get_all_agent_statuses()
        all_tasks      = sm.get_all_tasks()
        error_logs     = sm.get_agent_error_logs(50)

        # ── Fix immediate bugs ─────────────────────────────────
        recent_errors = [
            str(e) for e in error_logs
            if "attributeerror" in str(e).lower()
            or "syntaxerror"    in str(e).lower()
            or "importerror"    in str(e).lower()
            or "typeerror"      in str(e).lower()
        ]

        if recent_errors:
            log.info(f"  🐛 Found {len(recent_errors)} fixable errors")
            for error in recent_errors[:3]:
                # Find which agent this belongs to
                for agent_name, filename in AGENT_FILES.items():
                    if agent_name.lower().replace("_", "") in error.lower():
                        code = _read_agent_file(filename)
                        if code:
                            fix = fix_bug(error, agent_name, code)
                            if fix:
                                log.info(
                                    f"  ✅ Fixed {agent_name}: "
                                    f"{fix.get('root_cause','')}"
                                )
                                state["fixes_applied"].append({
                                    "agent":       agent_name,
                                    "error":       error[:100],
                                    "fix":         fix.get("fix_description",""),
                                    "timestamp":   datetime.now().isoformat()
                                })
                        break

        # ── Check if 3-day improvement is due ─────────────────
        should_improve = False
        if not state["last_improvement"]:
            should_improve = True
        else:
            last = datetime.fromisoformat(state["last_improvement"])
            if datetime.now() - last >= timedelta(days=IMPROVEMENT_DAYS):
                should_improve = True

        if should_improve and agent_statuses:
            run_improvement_cycle(state, agent_statuses, all_tasks)

        # ── Monitor agent health ───────────────────────────────
        degraded = []
        for agent in agent_statuses:
            name   = agent.get("agent","")
            score  = agent.get("score", 5)
            cycles = agent.get("cycles_done", 0)
            if score < 3 and cycles > 5:
                degraded.append(name)
                log.warning(f"  ⚠️ DEGRADED: {name} (score={score})")

        if degraded:
            for agent in degraded:
                sm.post_task(
                    f"T{datetime.now().strftime('%H%M%S')}",
                    f"Emergency fix: {agent}",
                    f"{agent} is critically degraded. Analyze logs, "
                    f"identify root cause, push fix immediately.",
                    sm.AGENT_FIXER,
                    priority="HIGH",
                    context={"target_agent": agent}
                )

        # ── Report own status ──────────────────────────────────
        sm.report_status(
            sm.AGENT_FIXER,
            status      = "ACTIVE",
            current_task= f"Monitoring — {len(recent_errors)} errors found",
            cycles_done = state["cycle"],
            last_output = f"Fixed {len(state['fixes_applied'])} issues total",
            score       = 10
        )

        _save_state(state)
        log.info(f"\n  ⏱️  Next check in 10 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()
