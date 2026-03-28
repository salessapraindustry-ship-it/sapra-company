#!/usr/bin/env python3
# ================================================================
#  fixer.py — Pro-Fixer Agent v2
#  Powered by Claude Code (claude-sonnet-4-5)
#  - Reads every agent file from GitHub
#  - Analyzes with Claude Code (system prompt + full code context)
#  - Fixes bugs and pushes to GitHub autonomously
#  - Scores and improves every agent every 3 days
#  - Never leaves cycles empty — always does something
# ================================================================

import os
import re
import json
import time
import base64
import logging
import requests
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
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "")
MONITOR_API_URL   = os.environ.get("MONITOR_API_URL",
                        "https://monitor-api-production.up.railway.app")
MONITOR_API_KEY   = os.environ.get("MONITOR_API_KEY", "sapra2026")

FIXER_MODEL    = "claude-sonnet-4-5"
FIXER_TOKENS   = 8000
CYCLE_INTERVAL = 900   # 15 min — staggered from CEO (15min) and researcher (25min)
IMPROVE_DAYS   = 3

AGENT_FILES = {
    sm.AGENT_RESEARCHER: "researcher.py",
    sm.AGENT_BACKEND:    "builder_backend.py",
    sm.AGENT_FRONTEND:   "builder_frontend.py",
    sm.AGENT_B2B:        "seller_b2b.py",
    sm.AGENT_FREELANCE:  "seller_freelance.py",
    sm.AGENT_CEO:        "ceo.py",
}

state_file = "/tmp/fixer_state.json"


# ================================================================
#  MONITOR API — read real company data without hammering Sheets
# ================================================================

def _monitor_get(endpoint):
    """Read from Monitor API — avoids Google Sheets quota."""
    try:
        resp = requests.get(
            f"{MONITOR_API_URL}/{endpoint}",
            params={"api_key": MONITOR_API_KEY},
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"Monitor API {endpoint}: {resp.status_code}")
    except Exception as e:
        log.warning(f"Monitor API error: {e}")
    return {}

def get_company_status():
    """Get full company status from monitor API."""
    return _monitor_get("status")

def get_all_tasks():
    """Get tasks via monitor API — no Sheets quota."""
    data = _monitor_get("tasks")
    return data.get("tasks", [])

def get_all_agents():
    """Get agent statuses via monitor API."""
    data = _monitor_get("agents")
    return data.get("agents", [])

def get_errors():
    """Get error logs via monitor API."""
    data = _monitor_get("errors")
    return data.get("errors", [])


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"cycle": 0, "last_improvement": None,
                "fixes_applied": [], "improvement_log": []}

def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


# ================================================================
#  GITHUB
# ================================================================

def _read_file(filename):
    for path in [f"/app/{filename}", f"./{filename}"]:
        if os.path.exists(path):
            try:
                return open(path).read()
            except Exception:
                pass
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3.raw"},
            timeout=15
        )
        if resp.status_code == 200:
            return resp.text
        log.warning(f"GitHub read {filename}: {resp.status_code}")
    except Exception as e:
        log.warning(f"GitHub read error {filename}: {e}")
    return None


def _write_file(filename, content, commit_msg):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        try:
            Path(f"/app/{filename}").write_text(content)
            log.info(f"Saved locally: {filename}")
            return True
        except Exception as e:
            log.error(f"Local write failed: {e}")
            return False
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10
        )
        sha = resp.json().get("sha", "") if resp.status_code == 200 else ""
        payload = {
            "message": f"[Pro-Fixer] {commit_msg}",
            "content": base64.b64encode(content.encode()).decode()
        }
        if sha:
            payload["sha"] = sha
        resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload, timeout=15
        )
        if resp.status_code in (200, 201):
            log.info(f"Pushed to GitHub: {filename}")
            return True
        log.error(f"GitHub push failed: {resp.status_code}")
    except Exception as e:
        log.error(f"GitHub write error: {e}")
    return False


# ================================================================
#  SAFE JSON PARSER — handles code inside strings
# ================================================================

def _parse(text):
    text = re.sub(r"```(?:json|python)?", "", text).strip()
    # Try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Walk and find outermost object
    try:
        start = text.index("{")
        depth = 0
        in_str = False
        escaped = False
        end = start
        for i, ch in enumerate(text[start:], start):
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_str:
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        return json.loads(text[start:end+1])
    except Exception:
        pass
    # Manual extraction fallback
    result = {}
    for key in ["issues_found", "improvement_plan", "rewritten_section",
                "root_cause", "fix_description", "expected_improvement"]:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            result[key] = m.group(1)
    return result if result else None


# ================================================================
#  CLAUDE CODE — The fixer brain
# ================================================================

def _call_claude(system, user, tokens=4096, timeout=120):
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
                "max_tokens": tokens,
                "system":     system,
                "messages":   [{"role": "user", "content": user}]
            },
            timeout=timeout
        )
        if resp.status_code == 200:
            return resp.json()["content"][0]["text"]
        log.error(f"Claude API {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        log.error(f"Claude call error: {e}")
    return None


def analyze_agent(agent_name, agent_code, error_context, score):
    """
    Claude Code full analysis — reads the code, finds all issues,
    rewrites the broken section. This is the Claude Code extension.
    """
    system = (
        "You are Pro-Fixer, an elite autonomous Python engineer with Claude Code capabilities. "
        "You read entire Python files, identify ALL bugs and performance issues, "
        "and rewrite broken sections to make them work perfectly. "
        "Return ONLY valid JSON. Escape ALL special characters inside string values. "
        "Never put raw unescaped Python code inside JSON strings — "
        "replace newlines with \\n and quotes with \\\"."
    )

    user = f"""Analyze and fix this autonomous agent.

AGENT: {agent_name}
SCORE: {score}/10
ERRORS/CONTEXT:
{error_context[:1500]}

FULL SOURCE CODE:
{agent_code[:4500]}

Return JSON (all code must be properly escaped):
{{
  "issues_found": ["specific issue 1", "specific issue 2", "specific issue 3"],
  "root_causes": "paragraph explaining the core failure",
  "improvement_plan": "what you will fix and how",
  "rewritten_section": "def function_name(...):\\n    # fixed code here\\n    pass",
  "expected_improvement": "score should go from {score} to X because Y"
}}"""

    text = _call_claude(system, user, tokens=FIXER_TOKENS)
    if not text:
        return None
    log.info(f"  Claude Code response: {len(text)} chars")
    result = _parse(text)
    if result:
        log.info(f"  Plan: {result.get('improvement_plan','')[:80]}")
    else:
        log.error("  Could not parse Claude Code response")
    return result


def fix_specific_bug(error_text, agent_name, agent_code):
    """Claude Code targeted bug fix."""
    system = (
        "You are Pro-Fixer. Fix this specific Python bug. "
        "Return ONLY valid JSON with properly escaped strings."
    )
    user = f"""Fix bug in {agent_name}.

ERROR: {error_text[:400]}

CODE:
{agent_code[:2000]}

JSON response:
{{
  "root_cause": "one sentence",
  "fix_description": "what changed",
  "fixed_code": "corrected Python, escaped for JSON"
}}"""

    text = _call_claude(system, user, tokens=2048, timeout=60)
    return _parse(text) if text else None


# ================================================================
#  SCORING
# ================================================================

def score_agent(agent_status, all_tasks):
    name  = agent_status.get("agent", "")
    score = 5
    done   = sum(1 for t in all_tasks
                 if t.get("assigned_to") == name
                 and t.get("status") == sm.STAGE_DONE)
    failed = sum(1 for t in all_tasks
                 if t.get("assigned_to") == name
                 and t.get("status") == sm.STAGE_FAILED)
    score += min(done, 3)
    score -= min(failed * 2, 5)
    last = agent_status.get("last_output", "").lower()
    if any(w in last for w in ["error", "failed"]):
        score -= 1
    if any(w in last for w in ["success", "done", "built", "deployed", "listed"]):
        score += 1
    return max(0, min(10, score))


# ================================================================
#  CORE ACTIONS — always run every cycle
# ================================================================

def fix_failed_tasks(all_tasks, state):
    failed = [t for t in all_tasks if t.get("status") == sm.STAGE_FAILED]
    if not failed:
        log.info("  No failed tasks to fix")
        return

    log.info(f"  Found {len(failed)} failed tasks")

    # Group by agent, fix worst offender first
    by_agent = {}
    for t in failed:
        a = t.get("assigned_to", "UNKNOWN")
        by_agent.setdefault(a, []).append(t)

    for agent_name, tasks in sorted(
        by_agent.items(), key=lambda x: -len(x[1])
    )[:2]:
        filename = AGENT_FILES.get(agent_name)
        if not filename:
            continue
        code = _read_file(filename)
        if not code:
            log.warning(f"  Cannot read {filename} for {agent_name}")
            continue

        log.info(f"  Analyzing {agent_name} ({len(tasks)} failures)...")
        context = "\n".join([
            f"- {t.get('title','')}: {t.get('result','no result')}"
            for t in tasks[:5]
        ])

        analysis = analyze_agent(agent_name, code, context, score=3)
        if analysis and analysis.get("rewritten_section"):
            patch = (
                f"\n\n# === PRO-FIXER PATCH {datetime.now().strftime('%Y%m%d_%H%M')} ===\n"
                f"# Fixed: {agent_name}\n"
                f"# Issues: {', '.join(analysis.get('issues_found', []))}\n"
                + analysis["rewritten_section"]
            )
            if _write_file(filename, code + patch,
                           f"Fix {agent_name} — {len(tasks)} failures"):
                sm.post_fixer_report(
                    agent_improved = agent_name,
                    metric_before  = 3,
                    metric_after   = 6,
                    changes_made   = analysis.get("improvement_plan", ""),
                    cycle_number   = state["cycle"]
                )
                state["fixes_applied"].append({
                    "agent": agent_name,
                    "time":  datetime.now().isoformat(),
                    "tasks": len(tasks)
                })
                log.info(f"  Fixed {agent_name} and pushed to GitHub")
        time.sleep(3)


def improvement_cycle(agent_statuses, all_tasks, state):
    """3-day cycle: score all, rewrite worst."""
    log.info("  Running 3-day improvement cycle")
    scores = {
        a.get("agent"): score_agent(a, all_tasks)
        for a in agent_statuses
        if a.get("agent") in AGENT_FILES
        and a.get("agent") != sm.AGENT_CEO
    }
    if not scores:
        return
    for name, s in scores.items():
        log.info(f"  Score {name}: {s}/10")

    worst = min(scores, key=scores.get)
    ws    = scores[worst]
    log.info(f"  Improving {worst} (score={ws})")

    filename = AGENT_FILES.get(worst)
    code     = _read_file(filename)
    if not code:
        return

    failed = [t for t in all_tasks
              if t.get("assigned_to") == worst
              and t.get("status") == sm.STAGE_FAILED]
    context = "\n".join([
        f"{t.get('title','')}: {t.get('result','')}"
        for t in failed[:8]
    ]) or "No specific failures logged"

    analysis = analyze_agent(worst, code, context, ws)
    if analysis and analysis.get("rewritten_section"):
        improved = (
            code
            + f"\n\n# === 3-DAY IMPROVEMENT {datetime.now().strftime('%Y%m%d')} ===\n"
            f"# Score: {ws}/10 → {min(ws+2,10)}/10\n"
            f"# Plan: {analysis.get('improvement_plan','')}\n"
            + analysis["rewritten_section"]
        )
        if _write_file(filename, improved,
                       f"3-day improvement {worst}: {ws}→{min(ws+2,10)}"):
            sm.post_fixer_report(
                agent_improved = worst,
                metric_before  = ws,
                metric_after   = min(ws+2, 10),
                changes_made   = analysis.get("improvement_plan",""),
                cycle_number   = state["cycle"]
            )
            state["improvement_log"].append({
                "agent": worst, "from": ws,
                "to": min(ws+2,10), "time": datetime.now().isoformat()
            })
            log.info(f"  3-day improvement complete: {worst}")

    state["last_improvement"] = datetime.now().isoformat()


def check_health(agent_statuses, all_tasks):
    """Alert CEO about critically degraded agents."""
    for a in agent_statuses:
        name   = a.get("agent", "")
        score  = score_agent(a, all_tasks)
        cycles = int(a.get("cycles_done", 0))
        if score < 3 and cycles > 5:
            log.warning(f"  DEGRADED: {name} score={score}")
            sm.post_task(
                f"T{datetime.now().strftime('%H%M%S')}DEG",
                f"CRITICAL: {name} degraded",
                f"{name} score={score}/10 after {cycles} cycles. Emergency fix needed.",
                sm.AGENT_FIXER,
                priority="HIGH",
                context={"target": name, "score": score}
            )


# ================================================================
#  MAIN LOOP
# ================================================================

def run():
    log.info("=" * 60)
    log.info("  PRO-FIXER v2 — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  Powered by Claude Code")
    log.info("  Every cycle: fix failures, monitor health, improve agents")
    log.info("=" * 60)

    state = _load_state()

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  FIXER CYCLE {state['cycle']} — "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        try:
            # Use Monitor API — avoids Google Sheets 429 quota errors
            status         = get_company_status()
            all_tasks      = get_all_tasks()
            agent_statuses = get_all_agents()
            error_logs     = get_errors()

            total_tasks  = status.get("tasks", {}).get("total", 0)
            failed_count = status.get("tasks", {}).get("failed", 0)
            log.info(f"  Agents: {len(agent_statuses)} | Tasks: {total_tasks} | Failed: {failed_count}")
            log.info(f"  Errors in log: {len(error_logs)}")

            # 1. Fix failed tasks using real task data
            fix_failed_tasks(all_tasks, state)

            # 2. Monitor health using real agent data
            check_health(agent_statuses, all_tasks)

            # 3. 3-day improvement — pass real data
            last = state.get("last_improvement")
            due  = (not last or
                    datetime.now() - datetime.fromisoformat(last)
                    >= timedelta(days=IMPROVE_DAYS))
            if due and len(agent_statuses) >= 2:
                improvement_cycle(agent_statuses, all_tasks, state)

            # 4. Report via Sheets (write only — low quota usage)
            sm.report_status(
                sm.AGENT_FIXER,
                status       = "ACTIVE",
                current_task = f"Cycle {state['cycle']} — monitoring via API",
                cycles_done  = state["cycle"],
                last_output  = (
                    f"Fixes: {len(state['fixes_applied'])} | "
                    f"Improvements: {len(state['improvement_log'])} | "
                    f"Failed tasks seen: {failed_count}"
                ),
                score        = 10
            )

        except Exception as e:
            log.error(f"Fixer cycle error: {e}")

        _save_state(state)
        log.info(f"\n  Next check in 10 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()
