#!/usr/bin/env python3
# ================================================================
#  ceo.py — CEO Agent
#  Claude Sonnet — reads all reports, assigns tasks, reviews output
#  Runs every 15 minutes, coordinates entire company
# ================================================================

import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timedelta

import shared_memory as sm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CEO_MODEL         = "claude-sonnet-4-5"
CEO_TOKENS        = 2048
CYCLE_INTERVAL    = 900   # 15 minutes
DAILY_TARGET      = 100   # USD per day

# ── Task ID generator ─────────────────────────────────────────────
def _task_id():
    return f"T{datetime.now().strftime('%m%d%H%M%S')}"


# ── Brain ─────────────────────────────────────────────────────────
def think(company_status):
    """CEO thinks about what the company should do next."""

    tasks     = company_status["tasks"]
    agents    = company_status["agents"]
    research  = company_status["research"]
    revenue   = company_status["revenue"]
    errors    = company_status["errors"]

    pending   = [t for t in tasks if t.get("status") == sm.STAGE_PENDING]
    active    = [t for t in tasks if t.get("status") not in
                 (sm.STAGE_DONE, sm.STAGE_FAILED, sm.STAGE_PENDING)]
    done      = [t for t in tasks if t.get("status") == sm.STAGE_DONE]
    failed    = [t for t in tasks if t.get("status") == sm.STAGE_FAILED]

    agent_summary = "\n".join([
        f"  {a.get('agent')}: {a.get('status')} | score={a.get('score',0)} | "
        f"last={a.get('last_output','')[:60]}"
        for a in agents
    ]) or "  No agents reporting yet."

    research_summary = "\n".join([
        f"  [{r.get('topic')}] {r.get('summary','')[:80]} (confidence={r.get('confidence',0)})"
        for r in research[-3:]
    ]) or "  No research yet."

    error_summary = "\n".join([
        str(e)[:80] for e in errors[-5:]
    ]) or "  No errors."

    prompt = f"""You are the CEO of an autonomous AI company. Your team earns money by building and selling software tools.

COMPANY STATUS:
- Total revenue: ${revenue:.2f}
- Daily target: ${DAILY_TARGET}
- Active tasks: {len(active)}
- Pending tasks: {len(pending)}
- Completed tasks: {len(done)}
- Failed tasks: {len(failed)}

AGENT STATUS:
{agent_summary}

LATEST RESEARCH:
{research_summary}

RECENT ERRORS:
{error_summary}

YOUR TEAM:
- DEEP_RESEARCHER: finds validated market opportunities, real buyer data, pricing benchmarks
- BACKEND_BUILDER: builds APIs, automation tools, data pipelines, deploys to Railway/Render
- FRONTEND_BUILDER: builds landing pages, dashboards, UIs that convert buyers
- B2B_SELLER: lists tools on RapidAPI/AppSumo, sets up Stripe subscriptions, passive income
- FREELANCE_SELLER: bids on Toptal/Upwork high-ticket projects ($500-5000), LinkedIn outreach
- PRO_FIXER: monitors all agents, fixes bugs, improves underperformers every 3 days

DECISION RULES:
- Always have DEEP_RESEARCHER working on at least 1 research task
- Don't assign BUILD tasks until research confirms demand
- BACKEND_BUILDER and FRONTEND_BUILDER work as a pair on same product
- After a tool is built, IMMEDIATELY assign to both sellers
- If an agent has score < 3 and 5+ cycles done → alert PRO_FIXER
- Max 3 active tasks per agent at once
- Focus on products that can generate recurring revenue

TASK FORMAT — reply ONLY in JSON:
{{
  "analysis": "1-2 sentence company assessment",
  "decisions": [
    {{
      "action": "assign_task|alert_fixer|wait|reassign",
      "task_id": "auto",
      "title": "short task title",
      "description": "detailed task description (what to build/do/research)",
      "assigned_to": "AGENT_NAME",
      "priority": "HIGH|NORMAL|LOW",
      "context": {{}}
    }}
  ],
  "company_health": "GOOD|NEEDS_ATTENTION|CRITICAL"
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
                "model":      CEO_MODEL,
                "max_tokens": CEO_TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"].strip()
            text = re.sub(r"```json|```", "", text).strip()
            # Extract JSON
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
        else:
            log.error(f"CEO API error: {resp.status_code}")
            return None
    except Exception as e:
        log.error(f"CEO think error: {e}")
        return None


def execute(decisions):
    """Execute CEO decisions."""
    if not decisions:
        return

    for d in decisions:
        action = d.get("action", "")

        if action == "assign_task":
            task_id = _task_id()
            sm.post_task(
                task_id     = task_id,
                title       = d.get("title", ""),
                description = d.get("description", ""),
                assigned_to = d.get("assigned_to", ""),
                priority    = d.get("priority", "NORMAL"),
                context     = d.get("context", {})
            )

        elif action == "alert_fixer":
            task_id = _task_id()
            sm.post_task(
                task_id     = task_id,
                title       = f"URGENT: Fix {d.get('assigned_to','')}",
                description = d.get("description", "Agent underperforming"),
                assigned_to = sm.AGENT_FIXER,
                priority    = "HIGH",
                context     = {"target_agent": d.get("assigned_to", "")}
            )

        elif action == "reassign":
            sm.update_task(d.get("task_id",""), sm.STAGE_PENDING)
            new_task_id = _task_id()
            sm.post_task(
                task_id     = new_task_id,
                title       = d.get("title", "Reassigned task"),
                description = d.get("description", ""),
                assigned_to = d.get("assigned_to", ""),
                priority    = d.get("priority", "NORMAL"),
                context     = d.get("context", {})
            )

        time.sleep(0.5)


def run():
    """Main CEO loop."""
    log.info("=" * 60)
    log.info("  CEO AGENT — AWAKENING")
    log.info(f"  {datetime.now()}")
    log.info("  I lead. I delegate. I drive results.")
    log.info("=" * 60)

    cycle = 0

    # Seed initial tasks on first run
    sm.post_task(
        _task_id(),
        "Market research — find top 5 profitable tool ideas",
        "Research RapidAPI marketplace, AppSumo, ProductHunt for gaps. "
        "Find tools with 100+ buyers, $10-99/month price range, "
        "that can be built in 1-2 days. Validate with Reddit/HN demand.",
        sm.AGENT_RESEARCHER,
        priority="HIGH"
    )
    sm.post_task(
        _task_id(),
        "Find 3 active high-ticket freelance projects",
        "Search Toptal, Upwork Enterprise, Gun.io for Python automation, "
        "API development, or data pipeline projects. Budget must be $500+. "
        "Collect project details, client info, and requirements.",
        sm.AGENT_FREELANCE,
        priority="HIGH"
    )

    while True:
        cycle += 1
        log.info(f"\n{'='*60}")
        log.info(f"  CEO CYCLE {cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        # Read company state
        company_status = {
            "tasks":    sm.get_all_tasks(),
            "agents":   sm.get_all_agent_statuses(),
            "research": sm.get_latest_research(5),
            "revenue":  sm.get_total_revenue(),
            "errors":   sm.get_agent_error_logs(20),
        }

        revenue = company_status["revenue"]
        log.info(f"  💰 Total revenue: ${revenue:.2f}")
        log.info(f"  📋 Tasks: {len(company_status['tasks'])} total")
        log.info(f"  👥 Agents reporting: {len(company_status['agents'])}")

        # Think
        log.info("  🧠 CEO thinking...")
        decision = think(company_status)

        if decision:
            log.info(f"  📊 Analysis: {decision.get('analysis','')}")
            log.info(f"  🏢 Health: {decision.get('company_health','')}")
            decisions = decision.get("decisions", [])
            log.info(f"  📋 Decisions: {len(decisions)}")
            execute(decisions)

        # Report own status
        sm.report_status(
            sm.AGENT_CEO,
            status       = "ACTIVE",
            current_task = f"Cycle {cycle}",
            cycles_done  = cycle,
            last_output  = decision.get("analysis","") if decision else "thinking",
            score        = 10
        )

        log.info(f"\n  ⏱️  Next cycle in 15 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()
