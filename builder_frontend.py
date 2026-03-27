#!/usr/bin/env python3
# ================================================================
#  builder_frontend.py — Frontend Builder Agent
#  Builds landing pages, dashboards, UIs that convert buyers
#  Deploys to GitHub Pages / Netlify automatically
# ================================================================

import os
import re
import json
import time
import logging
import requests
from datetime import datetime
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
GITHUB_USERNAME   = os.environ.get("GITHUB_USERNAME", "")
MODEL             = "claude-haiku-4-5-20251001"
TOKENS            = 2048
CYCLE_INTERVAL    = 1800  # 30 minutes

state_file = "/tmp/frontend_builder_state.json"


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"cycle": 0, "built_pages": []}


def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def generate_landing_page(tool_name, description, price,
                          endpoints, repo_url):
    """Generate a high-converting landing page for a tool."""
    prompt = f"""You are an expert frontend developer and copywriter.
Create a high-converting landing page for a developer tool.

TOOL: {tool_name}
DESCRIPTION: {description}
PRICE: {price}
API ENDPOINTS: {', '.join(endpoints[:3])}
REPO: {repo_url}

Build a single HTML file (index.html) with:
1. Compelling headline focused on the benefit, not the feature
2. 3 key features with icons (use emoji)
3. Code snippet showing how easy it is to use
4. Pricing section with clear CTA button
5. Simple footer with GitHub link
6. Modern, clean design using Tailwind CSS CDN
7. Mobile responsive

The page should make a developer want to buy this tool immediately.

Reply in JSON:
{{
  "index_html": "complete single-file HTML",
  "headline": "the main hero headline",
  "tagline": "1 sentence value prop",
  "cta_text": "button text",
  "estimated_conversion": "X% of visitors buy"
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
        log.error(f"generate_landing_page error: {e}")
    return None


def deploy_to_github_pages(tool_name, html_content):
    """Deploy landing page to GitHub Pages."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        log.info(f"  ✅ Page saved locally: {path}")
        return f"local:/tmp/pages/{tool_name}"

    try:
        import base64
        repo_name  = f"{tool_name}-landing"
        pages_url  = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"

        # Create or update repo
        requests.post(
            "https://api.github.com/user/repos",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            json={"name": repo_name, "auto_init": True},
            timeout=10
        )
        time.sleep(1)

        # Push index.html
        get_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10
        )
        sha     = get_resp.json().get("sha","") if get_resp.status_code == 200 else ""
        payload = {
            "message": f"[Frontend Builder] Deploy {tool_name} landing page",
            "content": base64.b64encode(html_content.encode()).decode()
        }
        if sha:
            payload["sha"] = sha

        requests.put(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            json=payload,
            timeout=10
        )

        # Enable GitHub Pages
        requests.post(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            json={"source": {"branch": "main", "path": "/"}},
            timeout=10
        )

        log.info(f"  ✅ Landing page live: {pages_url}")
        return pages_url
    except Exception as e:
        log.error(f"deploy_to_github_pages error: {e}")
        return ""


def build_frontend(task):
    """Execute a frontend build task."""
    log.info(f"  🎨 Building frontend: {task.get('title','')}")
    sm.update_task(task["task_id"], sm.STAGE_BUILD)

    context    = json.loads(task.get("context","{}")) if isinstance(
                     task.get("context"), str) else task.get("context", {})
    tool_name  = context.get("tool_name", "tool")
    description= context.get("description", task.get("description",""))
    price      = context.get("price", "$29/month")
    endpoints  = context.get("endpoints", [])
    repo_url   = context.get("repo_url", "")

    # Generate landing page
    result = generate_landing_page(
        tool_name, description, price, endpoints, repo_url
    )

    if not result:
        sm.update_task(task["task_id"], sm.STAGE_FAILED, "Generation failed")
        return None

    log.info(f"  📝 Headline: {result.get('headline','')}")
    log.info(f"  📈 Est. conversion: {result.get('estimated_conversion','?')}")

    # Deploy
    sm.update_task(task["task_id"], sm.STAGE_DEPLOY)
    page_url = deploy_to_github_pages(
        tool_name,
        result.get("index_html", "<html><body>Coming soon</body></html>")
    )

    result_summary = (
        f"DEPLOYED: {page_url} | "
        f"Headline: {result.get('headline','')[:60]} | "
        f"CTA: {result.get('cta_text','Buy Now')}"
    )
    sm.update_task(task["task_id"], sm.STAGE_DONE, result_summary)

    # Update sellers with the landing page URL
    sm.post_task(
        f"T{datetime.now().strftime('%H%M%S')}FE",
        f"Landing page ready: {tool_name}",
        f"Landing page is live at {page_url}. "
        f"Use this URL in all listings and outreach.",
        sm.AGENT_B2B,
        priority="HIGH",
        context={**context, "landing_page_url": page_url}
    )

    return result


def run():
    """Main Frontend Builder loop."""
    log.info("=" * 60)
    log.info("  FRONTEND BUILDER — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  I build pages that make people click Buy. Clean. Fast. Converts.")
    log.info("=" * 60)

    state = _load_state()

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  FRONTEND CYCLE {state['cycle']} — "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        tasks = sm.get_my_tasks(sm.AGENT_FRONTEND)
        log.info(f"  📋 Tasks: {len(tasks)}")

        if tasks:
            for task in tasks[:1]:
                result = build_frontend(task)
                if result:
                    state["built_pages"].append(
                        task.get("title","")
                    )
        else:
            log.info("  ⏳ Waiting for build tasks from CEO")

        sm.report_status(
            sm.AGENT_FRONTEND,
            status       = "ACTIVE",
            current_task = f"Built {len(state['built_pages'])} pages total",
            cycles_done  = state["cycle"],
            last_output  = f"Pages: {', '.join(state['built_pages'][-3:])}",
            score        = min(7 + len(state["built_pages"]), 10)
        )

        _save_state(state)
        log.info(f"\n  ⏱️  Next cycle in 30 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()
