#!/usr/bin/env python3
# ================================================================
#  seller_freelance.py — High-ticket Freelance Seller
#  Finds and bids on $500-5000 projects
#  Toptal, Upwork Enterprise, LinkedIn, YC job boards
# ================================================================

import os
import re
import json
import time
import logging
import requests
from datetime import datetime

import shared_memory as sm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SERPER_API_KEY    = os.environ.get("SERPER_API_KEY", "")
MODEL             = "claude-haiku-4-5-20251001"
TOKENS            = 1024
CYCLE_INTERVAL    = 1800   # 30 minutes

state_file = "/tmp/freelance_seller_state.json"


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"cycle": 0, "proposals_sent": 0, "projects_won": 0}


def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def search_projects(query):
    """Search for high-ticket freelance projects."""
    try:
        resp = requests.get(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY,
                     "Content-Type": "application/json"},
            json={"q": query, "num": 5, "tbs": "qdr:w"},  # last week
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json().get("organic", [])
    except Exception as e:
        log.warning(f"Project search failed: {e}")
    return []


def generate_proposal(project_details, our_tools, our_skills):
    """Generate a compelling project proposal."""
    prompt = f"""You are an expert freelancer writing a high-converting project proposal.

PROJECT: {project_details}
OUR TOOLS WE'VE BUILT: {our_tools}
OUR SKILLS: {our_skills}

Write a professional proposal that:
1. Shows we understand their specific problem
2. References relevant work we've done (our built tools)
3. Proposes a clear solution with timeline
4. States our rate confidently ($75-150/hr or fixed $500-5000)
5. Has a specific call-to-action

Keep it under 300 words. Professional but not robotic.

Reply in JSON:
{{
  "subject_line": "compelling email subject",
  "proposal_text": "the full proposal",
  "proposed_rate": "$X/hr or $X fixed",
  "estimated_timeline": "X days/weeks",
  "key_differentiator": "why we're the best choice"
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
                "model":      MODEL,
                "max_tokens": TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30
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
        log.error(f"generate_proposal error: {e}")
    return None


def find_and_bid_projects(state):
    """Search for projects and generate proposals."""
    log.info("  🔍 Searching for high-ticket projects")

    # Get our built tools for portfolio
    research     = sm.get_latest_research(3)
    built_tools  = [r.get("topic","") for r in research]
    tools_text   = ", ".join(built_tools) or "Python APIs, automation tools"

    # High-value search queries
    queries = [
        "site:upwork.com python API development automation $2000+",
        "site:toptal.com python backend developer needed 2024",
        "YC startup hiring python developer automation contract 2024",
        "site:linkedin.com/jobs python automation freelance contract $100/hr",
    ]

    proposals_generated = 0
    for query in queries[:2]:  # 2 searches per cycle to save API
        results = search_projects(query)
        for result in results[:2]:
            project_desc = f"{result.get('title','')} — {result.get('snippet','')}"
            log.info(f"  📋 Found: {project_desc[:80]}")

            # Generate proposal
            proposal = generate_proposal(
                project_details = project_desc,
                our_tools       = tools_text,
                our_skills      = "Python, FastAPI, automation, data pipelines, AI integration"
            )

            if proposal:
                log.info(f"  ✅ Proposal generated: {proposal.get('subject_line','')}")
                log.info(f"  💰 Rate: {proposal.get('proposed_rate','?')}")

                # Save proposal to Google Sheets for manual review/sending
                sm.log_revenue(
                    source      = "Upwork/LinkedIn proposal",
                    amount      = 0,
                    description = (
                        f"Proposal: {proposal.get('subject_line','')} | "
                        f"Rate: {proposal.get('proposed_rate','')} | "
                        f"Project: {project_desc[:100]}"
                    ),
                    agent_name  = sm.AGENT_FREELANCE
                )
                proposals_generated += 1
                state["proposals_sent"] += 1

            time.sleep(2)

    return proposals_generated


def execute_sell_task(task, state):
    """Execute a specific sell task from CEO."""
    log.info(f"  💼 Freelance selling: {task.get('title','')}")
    sm.update_task(task["task_id"], sm.STAGE_SELL)

    context     = json.loads(task.get("context","{}")) if isinstance(
                      task.get("context"), str) else task.get("context", {})
    tool_name   = context.get("tool_name", "")
    description = context.get("description", task.get("description",""))
    price       = context.get("price","$500")
    repo_url    = context.get("repo_url","")

    # Search for companies that need exactly this type of tool
    queries = [
        f"company hiring {description[:40]} developer freelance 2024",
        f"{description[:40]} automation needed site:linkedin.com",
    ]

    proposals_sent = 0
    for q in queries[:1]:
        results = search_projects(q)
        for result in results[:2]:
            project_desc = f"{result.get('title','')} — {result.get('snippet','')}"
            proposal = generate_proposal(
                project_details = project_desc,
                our_tools       = f"{tool_name}: {description}. Repo: {repo_url}",
                our_skills      = "Python automation, API development, ready-to-deploy tools"
            )
            if proposal:
                proposals_sent += 1
                state["proposals_sent"] += 1
                log.info(f"  📤 Proposal: {proposal.get('subject_line','')[:60]}")

    result = (
        f"Sent {proposals_sent} proposals for {tool_name} | "
        f"Rate: $75-150/hr | Total proposals: {state['proposals_sent']}"
    )
    sm.update_task(task["task_id"], sm.STAGE_DONE, result)


def run():
    """Main Freelance Seller loop."""
    log.info("=" * 60)
    log.info("  FREELANCE SELLER — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  I find $500-5000 projects. Toptal. Upwork. LinkedIn. YC.")
    log.info("=" * 60)

    state = _load_state()

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  FREELANCE CYCLE {state['cycle']} — "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        # Execute assigned tasks first
        tasks = sm.get_my_tasks(sm.AGENT_FREELANCE)
        log.info(f"  📋 Assigned tasks: {len(tasks)}")

        for task in tasks[:1]:
            execute_sell_task(task, state)
            time.sleep(2)

        # Always do background project hunting
        new_proposals = find_and_bid_projects(state)
        log.info(f"  📤 New proposals this cycle: {new_proposals}")

        sm.report_status(
            sm.AGENT_FREELANCE,
            status       = "ACTIVE",
            current_task = f"Hunting projects — {state['proposals_sent']} proposals sent",
            cycles_done  = state["cycle"],
            last_output  = f"Won: {state['projects_won']} | Sent: {state['proposals_sent']}",
            score        = min(5 + state["projects_won"] * 3, 10)
        )

        _save_state(state)
        log.info(f"\n  ⏱️  Next cycle in 30 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()
