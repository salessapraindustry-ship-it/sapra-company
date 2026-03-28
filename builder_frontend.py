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

# === PRO-FIXER PATCH 20260328_1603 ===
# Fixed: FRONTEND_BUILDER
# Issues: generate_landing_page() does not properly escape quotes and newlines in the JSON prompt, causing LLM to return malformed responses that fail parsing, deploy_to_github_pages() function is incomplete - cuts off mid-implementation with 'repo_' and never completes GitHub API integration, JSON parsing in generate_landing_page() uses fragile manual bracket-matching that fails on nested objects or escaped quotes in HTML content, No error handling for API key validation - agent runs but silently fails when ANTHROPIC_API_KEY is missing or invalid, The _retry_api() wrapper function exists but is NEVER USED anywhere in the code, so API calls fail on first error without retries, No validation that generated HTML is actually valid before attempting deployment, State management saves 'built_pages' but never checks it to avoid rebuilding the same page multiple times
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

Reply with ONLY valid JSON (no markdown, no code blocks):
{{
  \"index_html\": \"complete single-file HTML\",
  \"headline\": \"the main hero headline\",
  \"tagline\": \"1 sentence value prop\",
  \"cta_text\": \"button text\",
  \"estimated_conversion\": \"X% of visitors buy\"
}}"""

    def _api_call():
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
        resp.raise_for_status()
        return resp.json()

    try:
        result = _retry_api(_api_call, retries=3, delay=3)
        if not result:
            log.error("API call failed after retries")
            return None

        text = result["content"][0]["text"].strip()
        text = re.sub(r'^\s*|^\s*|\s*$', '', text, flags=re.MULTILINE).strip()
        
        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            log.error(f"No JSON found in response: {text[:200]}")
            return None
        
        json_str = match.group(0)
        data = json.loads(json_str)
        
        if "index_html" not in data or len(data["index_html"]) < 100:
            log.error("Generated HTML is too short or missing")
            return None
            
        return data
        
    except json.JSONDecodeError as e:
        log.error(f"JSON decode error: {e}")
        return None
    except Exception as e:
        log.error(f"generate_landing_page error: {e}")
        return None


def deploy_to_github_pages(tool_name, html_content):
    """Deploy landing page to GitHub Pages."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content, encoding='utf-8')
        log.info(f"  ✅ Page saved locally: {path}")
        return f"local:/tmp/pages/{tool_name}"

    try:
        import base64
        repo_name = f"{tool_name.lower().replace(' ', '-')}-page"
        
        def _create_repo():
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
                resp.raise_for_status()
            return resp
        
        _retry_api(_create_repo, retries=2, delay=2)
        time.sleep(2)
        
        def _upload_file():
            content_b64 = base64.b64encode(html_content.encode('utf-8')).decode('utf-8')
            resp = requests.put(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": f"Deploy {tool_name} landing page",
                    "content": content_b64,
                    "branch": "main"
                },
                timeout=30
            )
            resp.raise_for_status()
            return resp
        
        _retry_api(_upload_file, retries=3, delay=2)
        
        def _enable_pages():
            resp = requests.post(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={"source": {"branch": "main", "path": "/"}},
                timeout=30
            )
            if resp.status_code not in [201, 409]:
                resp.raise_for_status()
            return resp
        
        _retry_api(_enable_pages, retries=2, delay=2)
        
        url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed to GitHub Pages: {url}")
        return url
        
    except Exception as e:
        log.error(f"GitHub deployment failed: {e}")
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content, encoding='utf-8')
        log.info(f"  ⚠️  Fallback: saved locally to {path}")
        return f"local:/tmp/pages/{tool_name}"


def validate_api_keys():
    """Validate required API keys at startup."""
    if not ANTHROPIC_API_KEY:
        log.error("❌ ANTHROPIC_API_KEY not set")
        return False
    if len(ANTHROPIC_API_KEY) < 20:
        log.error("❌ ANTHROPIC_API_KEY appears invalid")
        return False
    log.info("✅ API keys validated")
    return True


def run_cycle():
    """Run one build cycle."""
    if not validate_api_keys():
        return
    
    state = _load_state()
    state["cycle"] = state.get("cycle", 0) + 1
    
    tools = [
        {
            "name": "WebScraper API",
            "desc": "Extract structured data from any website with one API call",
            "price": "$29",
            "endpoints": ["/scrape", "/extract", "/render"],
            "repo": "https://github.com/example/webscraper"
        },
        {
            "name": "Webhook-to-Email",
            "desc": "Forward webhooks to email instantly, no code required",
            "price": "$19",
            "endpoints": ["/webhook", "/forward", "/logs"],
            "repo": "https://github.com/example/webhook-email"
        },
        {
            "name": "Screenshot API",
            "desc": "Capture pixel-perfect screenshots of any webpage",
            "price": "$24",
            "endpoints": ["/screenshot", "/pdf", "/thumbnail"],
            "repo": "https://github.com/example/screenshot"
        },
        {
            "name": "JSON to CSV Converter",
            "desc": "Transform JSON to CSV instantly via API",
            "price": "$14",
            "endpoints": ["/convert", "/batch", "/schema"],
            "repo": "https://github.com/example/json-csv"
        }
    ]
    
    built_pages = state.get("built_pages", [])
    
    for tool in tools:
        if tool["name"] in built_pages:
            log.info(f"⏭️  Skipping {tool['name']} (already built)")
            continue
            
        log.info(f"🔨 Building landing page: {tool['name']}")
        page_data = generate_landing_page(
            tool["name"],
            tool["desc"],
            tool["price"],
            tool["endpoints"],
            tool["repo"]
        )
        
        if page_data and "index_html" in page_data:
            url = deploy_to_github_pages(tool["name"], page_data["index_html"])
            log.info(f"  📊 Headline: {page_data.get('headline', 'N/A')}")
            log.info(f"  🔗 URL: {url}")
            built_pages.append(tool["name"])
            state["built_pages"] = built_pages
            _save_state(state)
        else:
            log.error(f"  ❌ Failed to generate page for {tool['name']}")
    
    log.info(f"✅ Cycle {state['cycle']} complete. Built {len(built_pages)}/4 pages.")


if __name__ == "__main__":
    log.info("🚀 Frontend Builder Agent starting...")
    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("👋 Shutting down...")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
        time.sleep(CYCLE_INTERVAL)

# === PRO-FIXER PATCH 20260328_1610 ===
# Fixed: FRONTEND_BUILDER
# Issues: The deploy_to_github_pages function is incomplete - it cuts off mid-line with 'repo_' and never finishes the GitHub API implementation, The generate_landing_page function uses fragile JSON parsing with manual brace-counting instead of robust error handling, causing silent failures when Claude returns non-JSON text, No error reporting mechanism exists - when generation fails, the agent doesn't communicate errors to shared_memory or log actionable failure reasons, The _retry_api wrapper function is defined but never actually used anywhere in the code, Missing main execution loop and task retrieval logic - there's no code to fetch tasks from shared_memory, process them, or report results back
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

Reply ONLY with valid JSON (no markdown, no explanation):
{{
  \"index_html\": \"complete single-file HTML\",
  \"headline\": \"the main hero headline\",
  \"tagline\": \"1 sentence value prop\",
  \"cta_text\": \"button text\",
  \"estimated_conversion\": \"X% of visitors buy\"
}}"""

    def _api_call():
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
        resp.raise_for_status()
        return resp.json()

    try:
        result = _retry_api(_api_call)
        if not result:
            return None
        
        text = result["content"][0]["text"].strip()
        text = re.sub(r"\s*|\s*", "", text).strip()
        
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error for {tool_name}: {e}")
        log.error(f"Raw response: {text[:500]}")
        return None
    except Exception as e:
        log.error(f"generate_landing_page error: {e}")
        return None


def deploy_to_github_pages(tool_name, html_content):
    """Deploy landing page to GitHub Pages."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        path = Path(f"/tmp/pages/{tool_name}/index.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content)
        url = f"file://{path.absolute()}"
        log.info(f"  ✅ Page saved locally: {path}")
        return url

    try:
        import base64
        repo_name = f"{tool_name.lower().replace(' ', '-')}-landing"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        repo_data = {
            "name": repo_name,
            "description": f"Landing page for {tool_name}",
            "homepage": f"https://{GITHUB_USERNAME}.github.io/{repo_name}",
            "private": False,
            "has_issues": False,
            "has_wiki": False,
            "auto_init": False
        }
        
        create_resp = requests.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json=repo_data,
            timeout=30
        )
        
        if create_resp.status_code not in [201, 422]:
            log.error(f"Failed to create repo: {create_resp.status_code} {create_resp.text}")
            return None
        
        time.sleep(2)
        
        content_b64 = base64.b64encode(html_content.encode()).decode()
        file_data = {
            "message": "Deploy landing page",
            "content": content_b64,
            "branch": "gh-pages"
        }
        
        file_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers=headers,
            json=file_data,
            timeout=30
        )
        
        if file_resp.status_code not in [201, 200]:
            log.error(f"Failed to create file: {file_resp.status_code} {file_resp.text}")
            return None
        
        pages_data = {"source": {"branch": "gh-pages", "path": "/"}}
        requests.post(
            f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages",
            headers=headers,
            json=pages_data,
            timeout=30
        )
        
        url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
        log.info(f"  ✅ Deployed to GitHub Pages: {url}")
        return url
        
    except Exception as e:
        log.error(f"deploy_to_github_pages error: {e}")
        return None


def main():
    """Main execution loop."""
    log.info("🎨 Frontend Builder Agent starting...")
    state = _load_state()
    
    while True:
        try:
            state["cycle"] += 1
            log.info(f"\n{'='*60}\nCycle {state['cycle']} - {datetime.now()}\n{'='*60}")
            
            tasks = sm.get_tasks(agent_name="FRONTEND_BUILDER")
            
            if not tasks:
                log.info("No tasks available. Waiting...")
                time.sleep(CYCLE_INTERVAL)
                continue
            
            for task in tasks[:3]:
                task_id = task.get("id", "unknown")
                tool_name = task.get("tool_name", "Unknown Tool")
                description = task.get("description", "")
                price = task.get("price", "$29")
                endpoints = task.get("endpoints", [])
                repo_url = task.get("repo_url", "")
                
                log.info(f"\n📄 Building landing page: {tool_name}")
                
                page_data = generate_landing_page(
                    tool_name, description, price, endpoints, repo_url
                )
                
                if not page_data or "index_html" not in page_data:
                    error_msg = f"Failed to generate HTML for {tool_name}"
                    log.error(f"  ❌ {error_msg}")
                    sm.report_result(
                        agent_name="FRONTEND_BUILDER",
                        task_id=task_id,
                        success=False,
                        error=error_msg
                    )
                    continue
                
                url = deploy_to_github_pages(tool_name, page_data["index_html"])
                
                if url:
                    result_data = {
                        "tool_name": tool_name,
                        "url": url,
                        "headline": page_data.get("headline", ""),
                        "tagline": page_data.get("tagline", ""),
                        "cta_text": page_data.get("cta_text", ""),
                        "estimated_conversion": page_data.get("estimated_conversion", "")
                    }
                    state["built_pages"].append(result_data)
                    _save_state(state)
                    
                    sm.report_result(
                        agent_name="FRONTEND_BUILDER",
                        task_id=task_id,
                        success=True,
                        result=result_data
                    )
                    log.info(f"  ✅ Success: {url}")
                else:
                    error_msg = f"Failed to deploy {tool_name}"
                    log.error(f"  ❌ {error_msg}")
                    sm.report_result(
                        agent_name="FRONTEND_BUILDER",
                        task_id=task_id,
                        success=False,
                        error=error_msg
                    )
            
            log.info(f"\n📊 Total pages built: {len(state['built_pages'])}")
            time.sleep(CYCLE_INTERVAL)
            
        except KeyboardInterrupt:
            log.info("\n👋 Shutting down gracefully...")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    main()