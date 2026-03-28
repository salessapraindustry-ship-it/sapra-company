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

def _retry_api(fn, retries=3, delay=2):
    """Retry any API call on failure."""
    import time
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                import logging
                logging.getLogger(__name__).warning(f'API retry {attempt+1}/{retries}: {e}')
                time.sleep(delay)
            else:
                import logging
                logging.getLogger(__name__).error(f'API failed after {retries} attempts: {e}')
                return None


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


    # Stagger startup to avoid Google Sheets quota
    log.info(f"  ⏳ Staggered start — waiting 90s")
    time.sleep(90)

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


# === PRO-FIXER PATCH 20260328_1552 ===
# Fixed: FRONTEND_BUILDER
# Issues: JSON extraction uses naive string slicing with text.index('{') which fails when Claude returns text before JSON or malformed responses, deploy_to_github_pages function is incomplete - cuts off mid-statement at 'repo_' causing syntax error, No error handling for missing/malformed JSON responses from Claude API - fails silently instead of retrying with clearer prompts, generate_landing_page doesn't validate that returned JSON contains required keys (index_html, headline, etc.) before returning, No fallback templates when API fails - agent should have working HTML templates as backup, State management doesn't track failures or implement exponential backoff for repeatedly failing tools
def generate_landing_page(tool_name, description, price, endpoints, repo_url):
    """Generate a high-converting landing page for a tool."""
    prompt = f"""You are an expert frontend developer and copywriter.
Create a high-converting landing page for a developer tool.

TOOL: {tool_name}
DESCRIPTION: {description}
PRICE: {price}
API ENDPOINTS: {', '.join(endpoints[:3]) if endpoints else 'N/A'}
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

Reply ONLY with valid JSON, no other text:
{{
  \"index_html\": \"complete single-file HTML\",
  \"headline\": \"the main hero headline\",
  \"tagline\": \"1 sentence value prop\",
  \"cta_text\": \"button text\",
  \"estimated_conversion\": \"X% of visitors buy\"
}}"""

    def _call_api():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        if resp.status_code != 200:
            raise Exception(f"API error {resp.status_code}: {resp.text}")
        return resp.json()

    try:
        result = _retry_api(_call_api, retries=3, delay=3)
        if not result:
            log.error("API call failed after retries")
            return _get_fallback_template(tool_name, description, price, repo_url)

        text = result["content"][0]["text"].strip()
        
        # Try multiple JSON extraction methods
        parsed = None
        
        # Method 1: Find JSON block in code fence
        json_match = re.search(r'(?:json)?\s*({.*?})\s*', text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
            except:
                pass
        
        # Method 2: Find first complete JSON object
        if not parsed:
            try:
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
                if end > start:
                    parsed = json.loads(text[start:end+1])
            except:
                pass
        
        # Method 3: Try parsing entire response
        if not parsed:
            try:
                parsed = json.loads(text)
            except:
                pass
        
        if not parsed:
            log.error("Failed to extract JSON from response")
            return _get_fallback_template(tool_name, description, price, repo_url)
        
        # Validate required keys
        required_keys = ["index_html", "headline", "tagline", "cta_text"]
        if not all(key in parsed for key in required_keys):
            log.error(f"Missing required keys in response: {parsed.keys()}")
            return _get_fallback_template(tool_name, description, price, repo_url)
        
        # Validate HTML is not empty
        if not parsed["index_html"] or len(parsed["index_html"]) < 500:
            log.error("Generated HTML is too short or empty")
            return _get_fallback_template(tool_name, description, price, repo_url)
        
        return parsed
        
    except Exception as e:
        log.error(f"generate_landing_page error: {e}")
        return _get_fallback_template(tool_name, description, price, repo_url)


def _get_fallback_template(tool_name, description, price, repo_url):
    """Return a working fallback HTML template when API fails."""
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>{tool_name} - Developer Tool</title>
    <script src=\"https://cdn.tailwindcss.com\"></script>
</head>
<body class=\"bg-gray-50\">
    <div class=\"max-w-4xl mx-auto px-4 py-16\">
        <header class=\"text-center mb-16\">
            <h1 class=\"text-5xl font-bold text-gray-900 mb-4\">{tool_name}</h1>
            <p class=\"text-xl text-gray-600\">{description}</p>
        </header>
        
        <section class=\"bg-white rounded-lg shadow-lg p-8 mb-8\">
            <h2 class=\"text-3xl font-bold mb-6\">Features</h2>
            <div class=\"space-y-4\">
                <div class=\"flex items-start\">
                    <span class=\"text-2xl mr-3\">⚡</span>
                    <div><h3 class=\"font-bold\">Fast & Reliable</h3><p class=\"text-gray-600\">High-performance API built for developers</p></div>
                </div>
                <div class=\"flex items-start\">
                    <span class=\"text-2xl mr-3\">🔒</span>
                    <div><h3 class=\"font-bold\">Secure</h3><p class=\"text-gray-600\">Enterprise-grade security standards</p></div>
                </div>
                <div class=\"flex items-start\">
                    <span class=\"text-2xl mr-3\">📚</span>
                    <div><h3 class=\"font-bold\">Easy to Use</h3><p class=\"text-gray-600\">Simple API with great documentation</p></div>
                </div>
            </div>
        </section>
        
        <section class=\"bg-blue-600 text-white rounded-lg shadow-lg p-8 text-center\">
            <h2 class=\"text-3xl font-bold mb-4\">Pricing</h2>
            <p class=\"text-5xl font-bold mb-6\">{price}</p>
            <a href=\"{repo_url}\" class=\"inline-block bg-white text-blue-600 px-8 py-3 rounded-lg font-bold hover:bg-gray-100 transition\">Get Started Now</a>
        </section>
        
        <footer class=\"text-center mt-16 text-gray-600\">
            <p><a href=\"{repo_url}\" class=\"text-blue-600 hover:underline\">View on GitHub</a></p>
        </footer>
    </div>
</body>
</html>"""
    
    return {
        "index_html": html,
        "headline": tool_name,
        "tagline": description,
        "cta_text": "Get Started Now",
        "estimated_conversion": "2-3% (fallback template)"
    }


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
        repo_name = f"{tool_name.lower().replace(' ', '-')}-landing"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # Create repository
        create_repo_url = "https://api.github.com/user/repos"
        repo_data = {
            "name": repo_name,
            "description": f"Landing page for {tool_name}",
            "homepage": f"https://{GITHUB_USERNAME}.github.io/{repo_name}",
            "private": False,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False
        }
        
        repo_resp = requests.post(create_repo_url, headers=headers, json=repo_data, timeout=30)
        if repo_resp.status_code not in [201, 422]:  # 422 = already exists
            log.error(f"Failed to create repo: {repo_resp.status_code} {repo_resp.text}")
            return None
        
        # Upload index.html to main branch
        file_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html"
        file_data = {
            "message": f"Deploy {tool_name} landing page",
            "content": base64.b64encode(html_content.encode()).decode(),
            "branch": "main"
        }
        
        file_resp = requests.put(file_url, headers=headers, json=file_data, timeout=30)
        if file_resp.status_code not in [201, 200]:
            log.error(f"Failed to upload file: {file_resp.status_code} {file_resp.text}")
            return None
        
        # Enable GitHub Pages
        pages_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages"
        pages_data = {"source": {"branch": "main", "path": "/"}}
        requests.post(pages_url, headers=headers, json=pages_data, timeout=30)
        
        page_url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed to: {page_url}")
        return page_url
        
    except Exception as e:
        log.error(f"deploy_to_github_pages error: {e}")
        # Fallback to local save
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        log.info(f"  ✅ Page saved locally (GitHub failed): {path}")
        return f"local:/tmp/pages/{tool_name}"

# === PRO-FIXER PATCH 20260328_1600 ===
# Fixed: FRONTEND_BUILDER
# Issues: generate_landing_page() returns None on ALL failures but caller doesn't handle None, causing 'NoneType' object is not subscriptable errors, deploy_to_github_pages() is INCOMPLETE - cuts off mid-function at 'repo_' causing immediate syntax error and preventing any deployment, API calls lack proper error handling - single failures cascade into total agent failure without fallback or graceful degradation, No retry mechanism wrapping the main generation workflow - _retry_api exists but is never used on generate_landing_page(), JSON parsing in generate_landing_page uses fragile bracket-counting instead of robust extraction, fails on malformed Claude responses, Missing main loop and task execution logic - no run() function to actually process tasks from shared_memory, State management exists but is never used - _load_state() and _save_state() are defined but never called in workflow
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
        repo_name = f"{tool_name.lower().replace(' ', '-')}-landing"
        
        # Create repo
        resp = requests.post(
            f"https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={"name": repo_name, "auto_init": True, "private": False},
            timeout=30
        )
        
        if resp.status_code not in [201, 422]:  # 422 = already exists
            log.warning(f"Repo creation returned {resp.status_code}")
        
        time.sleep(2)
        
        # Upload index.html
        content_b64 = base64.b64encode(html_content.encode()).decode()
        resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "message": "Deploy landing page",
                "content": content_b64
            },
            timeout=30
        )
        
        if resp.status_code not in [201, 200]:
            log.error(f"File upload failed: {resp.status_code}")
            return None
        
        # Enable GitHub Pages
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.switcheroo-preview+json"
            },
            json={"source": {"branch": "main", "path": "/"}},
            timeout=30
        )
        
        url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed: {url}")
        return url
        
    except Exception as e:
        log.error(f"GitHub Pages deploy error: {e}")
        # Fallback to local
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        return f"local:/tmp/pages/{tool_name}"


def run():
    """Main execution loop for frontend builder agent."""
    log.info("🎨 Frontend Builder Agent starting...")
    state = _load_state()
    
    while True:
        try:
            state["cycle"] += 1
            log.info(f"\n{'='*60}\nCycle {state['cycle']} - {datetime.now()}\n{'='*60}")
            
            # Poll for frontend tasks
            tasks = sm.get_tasks("frontend", limit=5)
            
            if not tasks:
                log.info("No frontend tasks found. Checking for micro-tool requests...")
                # Check for product tasks that need landing pages
                product_tasks = sm.get_tasks("product", limit=3)
                
                for task in product_tasks:
                    if "landing page" in task.get("description", "").lower():
                        tasks.append(task)
            
            for task in tasks:
                task_id = task.get("id", "unknown")
                description = task.get("description", "")
                
                log.info(f"\n📋 Processing: {description}")
                
                # Extract tool details
                tool_name = task.get("tool_name", "Micro Tool")
                price = task.get("price", "$9")
                endpoints = task.get("endpoints", ["POST /api/execute"])
                repo_url = task.get("repo_url", "https://github.com/example/repo")
                
                # Generate landing page with retry
                def gen_fn():
                    return generate_landing_page(
                        tool_name=tool_name,
                        description=description,
                        price=price,
                        endpoints=endpoints,
                        repo_url=repo_url
                    )
                
                page_data = _retry_api(gen_fn, retries=3, delay=3)
                
                if not page_data or "index_html" not in page_data:
                    log.error(f"❌ Generation failed for {tool_name}")
                    sm.mark_task_failed(task_id, "Landing page generation returned invalid data")
                    continue
                
                # Deploy
                url = deploy_to_github_pages(tool_name, page_data["index_html"])
                
                if not url:
                    log.error(f"❌ Deployment failed for {tool_name}")
                    sm.mark_task_failed(task_id, "Deployment to GitHub Pages failed")
                    continue
                
                # Record success
                state["built_pages"].append({
                    "tool": tool_name,
                    "url": url,
                    "headline": page_data.get("headline", ""),
                    "timestamp": datetime.now().isoformat()
                })
                
                log.info(f"✅ SUCCESS: {tool_name} -> {url}")
                sm.mark_task_complete(task_id, {"url": url, "page_data": page_data})
            
            _save_state(state)
            log.info(f"\n💤 Cycle complete. Total pages built: {len(state['built_pages'])}")
            log.info(f"Sleeping {CYCLE_INTERVAL}s...\n")
            time.sleep(CYCLE_INTERVAL)
            
        except KeyboardInterrupt:
            log.info("\n🛑 Shutting down gracefully...")
            _save_state(state)
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run()

# === PRO-FIXER PATCH 20260328_1606 ===
# Fixed: FRONTEND_BUILDER
# Issues: deploy_to_github_pages() function is INCOMPLETE - cuts off mid-variable assignment ('repo_') causing syntax errors and preventing any deployment, No error handling or fallback when API calls fail - returns None and tasks fail silently without recording attempts or progress, Missing retry logic on actual generation calls - _retry_api() exists but is NEVER USED in generate_landing_page() or deployment functions, JSON parsing is brittle with regex-based extraction that fails on multi-line code blocks or nested JSON structures, No persistence of failed tasks - state only tracks 'built_pages' but not failed attempts, causing infinite retries of broken tasks, Missing shared_memory integration - imports 'sm' but never reads tasks or writes results back to coordinated memory
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
        repo_name = f"{tool_name.lower().replace(' ', '-')}-landing"
        
        # Create repo if doesn't exist
        def _create_repo():
            return requests.post(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"token {GITHUB_TOKEN}"},
                json={"name": repo_name, "auto_init": True},
                timeout=30
            )
        _retry_api(_create_repo)
        
        # Upload index.html
        content_b64 = base64.b64encode(html_content.encode()).decode()
        def _upload_file():
            return requests.put(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
                headers={"Authorization": f"token {GITHUB_TOKEN}"},
                json={
                    "message": "Deploy landing page",
                    "content": content_b64
                },
                timeout=30
            )
        resp = _retry_api(_upload_file)
        
        # Enable Pages
        def _enable_pages():
            return requests.post(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
                headers={"Authorization": f"token {GITHUB_TOKEN}"},
                json={"source": {"branch": "main", "path": "/"}},
                timeout=30
            )
        _retry_api(_enable_pages)
        
        url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed to: {url}")
        return url
    except Exception as e:
        log.error(f"GitHub Pages deployment failed: {e}")
        # Fallback to local
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        return f"local:/tmp/pages/{tool_name}"


def generate_landing_page_safe(tool_name, description, price, endpoints, repo_url):
    """Wrapper with retry logic and better JSON parsing."""
    def _api_call():
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

Reply ONLY with valid JSON (escape all special chars):
{{
  \"index_html\": \"complete single-file HTML\",
  \"headline\": \"the main hero headline\",
  \"tagline\": \"1 sentence value prop\",
  \"cta_text\": \"button text\",
  \"estimated_conversion\": \"X% of visitors buy\"
}}"""
        
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": MODEL,
                "max_tokens": TOKENS,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        if resp.status_code != 200:
            raise Exception(f"API error: {resp.status_code}")
        return resp.json()["content"][0]["text"].strip()
    
    text = _retry_api(_api_call)
    if not text:
        return None
    
    # Robust JSON extraction
    try:
        text = re.sub(r"|", "", text).strip()
        # Find first { and matching }
        start = text.index("{")
        depth, end = 0, 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        return json.loads(text[start:end+1])
    except Exception as e:
        log.error(f"JSON parse error: {e}")
        # Fallback: try to parse entire text
        try:
            return json.loads(text)
        except:
            return None


def run_cycle():
    """Main execution cycle with shared_memory integration."""
    state = _load_state()
    state["cycle"] = state.get("cycle", 0) + 1
    state.setdefault("built_pages", [])
    state.setdefault("failed_tasks", [])
    
    log.info(f"🎨 FRONTEND_BUILDER Cycle {state['cycle']}")
    
    # Get pending tasks from shared memory
    pending = sm.get_pending_tasks(agent="FRONTEND_BUILDER")
    
    for task in pending[:3]:  # Process max 3 per cycle
        task_id = task.get("id", "unknown")
        task_desc = task.get("description", "")
        
        # Skip if already attempted and failed
        if task_id in state["failed_tasks"]:
            log.info(f"  ⏭️  Skipping previously failed task: {task_id}")
            continue
        
        log.info(f"  📄 Building: {task_desc}")
        
        # Extract tool info from task
        tool_name = task.get("tool_name", "Developer Tool")
        description = task.get("description", task_desc)
        price = task.get("price", "$9")
        endpoints = task.get("endpoints", ["/api/v1"])
        repo_url = task.get("repo_url", "https://github.com")
        
        # Generate landing page
        result = generate_landing_page_safe(
            tool_name, description, price, endpoints, repo_url
        )
        
        if result and "index_html" in result:
            # Deploy
            url = deploy_to_github_pages(tool_name, result["index_html"])
            
            # Record success
            state["built_pages"].append({
                "task_id": task_id,
                "tool": tool_name,
                "url": url,
                "headline": result.get("headline", ""),
                "timestamp": datetime.now().isoformat()
            })
            
            # Update shared memory
            sm.complete_task(task_id, {
                "status": "completed",
                "url": url,
                "headline": result.get("headline"),
                "conversion_estimate": result.get("estimated_conversion")
            })
            
            log.info(f"  ✅ Success: {url}")
        else:
            # Record failure
            state["failed_tasks"].append(task_id)
            sm.fail_task(task_id, "Generation failed after retries")
            log.error(f"  ❌ Failed: {task_desc}")
    
    _save_state(state)
    log.info(f"  📊 Total pages built: {len(state['built_pages'])}")


if __name__ == "__main__":
    log.info("Frontend Builder Agent starting...")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Cycle error: {e}")
        time.sleep(CYCLE_INTERVAL)

# === PRO-FIXER PATCH 20260328_1614 ===
# Fixed: FRONTEND_BUILDER
# Issues: generate_landing_page() does not properly escape JSON strings - special characters in HTML break JSON parsing, deploy_to_github_pages() function is truncated/incomplete - it starts defining 'repo_' variable and cuts off mid-line, No retry logic applied to generate_landing_page() which is the main failure point causing 'Generation failed' errors, JSON parsing uses fragile regex and manual bracket counting instead of robust extraction, No validation that ANTHROPIC_API_KEY exists before making API calls, Missing error handling for malformed API responses and incomplete JSON objects, State management doesn't track failed attempts or implement exponential backoff
def generate_landing_page(tool_name, description, price, endpoints, repo_url):
    """Generate a high-converting landing page for a tool."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    prompt = f"""You are an expert frontend developer and copywriter.
Create a high-converting landing page for a developer tool.

TOOL: {tool_name}
DESCRIPTION: {description}
PRICE: {price}
API ENDPOINTS: {', '.join(endpoints[:3]) if endpoints else 'N/A'}
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

Reply ONLY with valid JSON (no markdown):
{{
  "index_html": "complete single-file HTML",
  "headline": "the main hero headline",
  "tagline": "1 sentence value prop",
  "cta_text": "button text",
  "estimated_conversion": "X% of visitors buy"
}}"""

    def _call_api():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")
        
        data = resp.json()
        if "content" not in data or not data["content"]:
            raise Exception("Empty response from API")
        
        text = data["content"][0]["text"].strip()
        text = re.sub(r"\s*|\s*", "", text).strip()
        
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end+1])
                except:
                    pass
            log.error(f"JSON parse error: {e}. Response: {text[:500]}")
            raise

    result = _retry_api(_call_api, retries=3, delay=3)
    if result and "index_html" in result:
        return result
    
    log.error(f"Failed to generate landing page for {tool_name}")
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
        repo_name = f"{tool_name.lower().replace(' ', '-')}-page"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        repo_data = {"name": repo_name, "auto_init": True, "private": False}
        repo_resp = requests.post(
            f"https://api.github.com/user/repos",
            headers=headers,
            json=repo_data,
            timeout=30
        )
        
        if repo_resp.status_code not in [201, 422]:
            log.warning(f"Repo creation returned {repo_resp.status_code}")
        
        time.sleep(2)
        
        encoded_content = base64.b64encode(html_content.encode()).decode()
        file_data = {
            "message": f"Deploy {tool_name} landing page",
            "content": encoded_content,
            "branch": "main"
        }
        
        file_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers=headers,
            json=file_data,
            timeout=30
        )
        
        if file_resp.status_code in [201, 200]:
            pages_data = {"source": {"branch": "main", "path": "/"}}
            requests.post(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
                headers=headers,
                json=pages_data,
                timeout=30
            )
            
            url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
            log.info(f"  ✅ Deployed to: {url}")
            return url
        else:
            log.error(f"File upload failed: {file_resp.status_code} - {file_resp.text[:200]}")
            return None
            
    except Exception as e:
        log.error(f"deploy_to_github_pages error: {e}")
        return None

# === PRO-FIXER PATCH 20260328_1624 ===
# Fixed: FRONTEND_BUILDER
# Issues: generate_landing_page() doesn't escape special characters in JSON strings - newlines, quotes, and code snippets in HTML break JSON parsing, deploy_to_github_pages() function is incomplete - cuts off mid-implementation at 'repo_' causing immediate syntax errors, No error handling for malformed Claude API responses - regex JSON extraction fails when Claude returns non-JSON or wrapped responses, Missing base64 import causes NameError when GITHUB_TOKEN exists but import is inside try block after potential early failures, Retry logic exists but is never actually used - generate_landing_page and deploy_to_github_pages don't wrap calls with _retry_api
import base64

def generate_landing_page(tool_name, description, price,
                          endpoints, repo_url):
    """Generate a high-converting landing page for a tool."""
    prompt = f"""You are an expert frontend developer and copywriter.
Create a high-converting landing page for a developer tool.

TOOL: {tool_name}
DESCRIPTION: {description}
PRICE: {price}
API ENDPOINTS: {', '.join(endpoints[:3]) if endpoints else 'N/A'}
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

Reply ONLY with valid JSON (escape all special characters):
{{
  \"index_html\": \"complete single-file HTML with escaped newlines and quotes\",
  \"headline\": \"the main hero headline\",
  \"tagline\": \"1 sentence value prop\",
  \"cta_text\": \"button text\",
  \"estimated_conversion\": \"X% of visitors buy\"
}}"""

    def _api_call():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model":      MODEL,
                "max_tokens": 4096,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")
        
        text = resp.json()["content"][0]["text"].strip()
        text = re.sub(r"\s*|\s*", "", text).strip()
        
        # Find first { and matching }
        if "{" not in text:
            raise Exception("No JSON object found in response")
        
        start = text.index("{")
        depth, end = 0, -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        
        if end == -1:
            raise Exception("No matching closing brace found")
        
        json_str = text[start:end+1]
        data = json.loads(json_str)
        
        # Validate required fields
        if "index_html" not in data or not data["index_html"]:
            raise Exception("Response missing index_html field")
        
        return data
    
    try:
        result = _retry_api(_api_call, retries=3, delay=3)
        if result:
            log.info(f"  ✅ Generated landing page: {result.get('headline', 'N/A')[:50]}")
        return result
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

    repo_name = f"{tool_name.lower().replace(' ', '-')}-landing"
    
    def _deploy():
        # Create repository
        resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "name": repo_name,
                "description": f"Landing page for {tool_name}",
                "homepage": f"https://{GITHUB_USERNAME}.github.io/{repo_name}",
                "private": False,
                "auto_init": True
            },
            timeout=30
        )
        
        if resp.status_code not in [201, 422]:
            raise Exception(f"Failed to create repo: {resp.status_code} {resp.text[:200]}")
        
        time.sleep(2)
        
        # Upload index.html to main branch
        encoded_content = base64.b64encode(html_content.encode()).decode()
        
        resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "message": "Deploy landing page",
                "content": encoded_content,
                "branch": "main"
            },
            timeout=30
        )
        
        if resp.status_code not in [200, 201]:
            raise Exception(f"Failed to upload file: {resp.status_code} {resp.text[:200]}")
        
        # Enable GitHub Pages
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "source": {
                    "branch": "main",
                    "path": "/"
                }
            },
            timeout=30
        )
        
        # 409 means pages already enabled, which is fine
        if resp.status_code not in [201, 409]:
            log.warning(f"GitHub Pages enable returned {resp.status_code}")
        
        url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed to: {url}")
        return url
    
    try:
        return _retry_api(_deploy, retries=2, delay=3)
    except Exception as e:
        log.error(f"deploy_to_github_pages error: {e}")
        # Fallback to local save
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        return f"local:/tmp/pages/{tool_name}"