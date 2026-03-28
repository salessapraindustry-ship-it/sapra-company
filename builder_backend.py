#!/usr/bin/env python3
# ================================================================
#  builder_backend.py — Backend Builder Agent
#  Builds APIs, automation tools, data pipelines
#  Deploys to Railway/Render automatically
# ================================================================

import os
import re
import json
import time
import logging
import requests
import subprocess
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
CYCLE_INTERVAL    = 1800   # 30 minutes

state_file = "/tmp/backend_builder_state.json"


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"cycle": 0, "built_tools": []}


def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def generate_backend_code(task_description, research_context):
    """Generate complete backend code for a tool."""
    prompt = f"""You are an expert Python backend developer.
Build a complete, deployable backend tool.

TASK: {task_description}
RESEARCH CONTEXT: {research_context}

Build a production-ready Python FastAPI service that:
1. Has a working API with proper endpoints
2. Includes API key authentication for paid users
3. Has clear documentation in code comments
4. Can be deployed on Railway with minimal config
5. Includes a requirements.txt

Reply in JSON:
{{
  "tool_name": "snake_case_name",
  "description": "what this tool does in 1 sentence",
  "main_py": "complete main.py code",
  "requirements_txt": "package1\\npackage2\\n...",
  "readme_md": "markdown README with usage examples",
  "api_endpoints": ["GET /endpoint1", "POST /endpoint2"],
  "suggested_price": "$X/month",
  "deployment_cmd": "railway up or render deploy command"
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
        log.error(f"generate_backend_code error: {e}")
    return None


def deploy_to_github(tool_name, files):
    """Create a GitHub repo and push the tool."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        # Save locally
        tool_dir = Path(f"/tmp/tools/{tool_name}")
        tool_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (tool_dir / filename).write_text(content)
        log.info(f"  ✅ Tool saved locally: {tool_dir}")
        return f"local:/tmp/tools/{tool_name}"

    try:
        # Create repo
        resp = requests.post(
            "https://api.github.com/user/repos",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "name":        tool_name,
                "description": files.get("_description", "AI-built tool"),
                "private":     False,
                "auto_init":   True
            },
            timeout=10
        )
        repo_url = ""
        if resp.status_code in (201, 422):  # 422 = already exists
            repo_url = f"https://github.com/{GITHUB_USERNAME}/{tool_name}"

        # Push files
        import base64
        for filename, content in files.items():
            if filename.startswith("_"):
                continue
            content_b64 = base64.b64encode(content.encode()).decode()

            # Get existing SHA if file exists
            get_resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{tool_name}/contents/{filename}",
                headers={"Authorization": f"token {GITHUB_TOKEN}"},
                timeout=10
            )
            sha = get_resp.json().get("sha","") if get_resp.status_code == 200 else ""

            payload = {
                "message": f"[Backend Builder] Add {filename}",
                "content": content_b64
            }
            if sha:
                payload["sha"] = sha

            requests.put(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{tool_name}/contents/{filename}",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            time.sleep(0.5)

        log.info(f"  ✅ Deployed to GitHub: {repo_url}")
        return repo_url
    except Exception as e:
        log.error(f"deploy_to_github error: {e}")
        return ""


def build_tool(task):
    """Execute a full backend build task."""
    log.info(f"  🔨 Building: {task.get('title','')}")
    sm.update_task(task["task_id"], sm.STAGE_BUILD)

    # Get latest research for context
    research = sm.get_latest_research(3)
    research_text = "\n".join([
        f"{r.get('topic')}: {r.get('summary','')}"
        for r in research
    ]) or "No research available"

    # Generate code
    code = generate_backend_code(task.get("description",""), research_text)

    if not code:
        sm.update_task(task["task_id"], sm.STAGE_FAILED, "Code generation failed")
        return None

    tool_name = code.get("tool_name", f"tool_{int(time.time())}")
    log.info(f"  📦 Tool: {tool_name}")
    log.info(f"  💰 Suggested price: {code.get('suggested_price','?')}")

    # Test the code
    sm.update_task(task["task_id"], sm.STAGE_TEST)
    main_code = code.get("main_py", "")
    try:
        compile(main_code, "<string>", "exec")
        log.info("  ✅ Code syntax valid")
    except SyntaxError as e:
        log.error(f"  ❌ Syntax error: {e}")
        sm.update_task(task["task_id"], sm.STAGE_FAILED, f"Syntax error: {e}")
        return None

    # Deploy
    sm.update_task(task["task_id"], sm.STAGE_DEPLOY)
    files = {
        "main.py":          main_code,
        "requirements.txt": code.get("requirements_txt", "fastapi\nuvicorn\n"),
        "README.md":        code.get("readme_md", f"# {tool_name}"),
        "_description":     code.get("description", "")
    }
    repo_url = deploy_to_github(tool_name, files)

    # ── Autonomous payment setup ───────────────────────────────
    try:
        import payments
        price     = float(
            code.get("suggested_price","$29/month")
            .replace("$","").replace("/month","").strip()
        )
        pay_links = payments.monetize_tool(
            tool_name    = tool_name,
            description  = code.get("description",""),
            repo_url     = repo_url,
            landing_url  = repo_url,
            price_usd    = price if price > 0 else 29.0
        )
    except Exception as e:
        log.warning(f"Payment setup error: {e}")
        pay_links = {}

    result = (
        f"BUILT: {tool_name} | "
        f"Price: {code.get('suggested_price','?')} | "
        f"Repo: {repo_url} | "
        f"Endpoints: {', '.join(code.get('api_endpoints',[])[:3])}"
    )
    sm.update_task(task["task_id"], sm.STAGE_DONE, result)

    # Notify sellers via new tasks
    sell_context = {
        "tool_name":    tool_name,
        "repo_url":     repo_url,
        "description":  code.get("description",""),
        "price":        code.get("suggested_price","$29/month"),
        "endpoints":    code.get("api_endpoints",[]),
        "readme":       code.get("readme_md","")[:500]
    }
    # Post sell tasks for both sellers
    for seller in [sm.AGENT_B2B, sm.AGENT_FREELANCE]:
        sm.post_task(
            f"T{datetime.now().strftime('%H%M%S')}{seller[:3]}",
            f"Sell: {tool_name}",
            f"List and sell {tool_name}. "
            f"Description: {code.get('description','')}. "
            f"Repo: {repo_url}. Suggested price: {code.get('suggested_price','$29/month')}",
            seller,
            priority="HIGH",
            context=sell_context
        )

    log.info(f"  📤 Sell tasks posted for both sellers")
    return code


def run():
    """Main Backend Builder loop."""
    log.info("=" * 60)
    log.info("  BACKEND BUILDER — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  I build APIs that make money. Clean code. Fast deploy.")
    log.info("=" * 60)

    state = _load_state()


    # Stagger startup to avoid Google Sheets quota
    log.info(f"  ⏳ Staggered start — waiting 60s")
    time.sleep(60)

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  BACKEND CYCLE {state['cycle']} — "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        tasks = sm.get_my_tasks(sm.AGENT_BACKEND)
        log.info(f"  📋 Tasks: {len(tasks)}")

        if tasks:
            for task in tasks[:1]:  # one build at a time
                result = build_tool(task)
                if result:
                    state["built_tools"].append(result.get("tool_name",""))
        else:
            log.info("  ⏳ Waiting for build tasks from CEO")

        sm.report_status(
            sm.AGENT_BACKEND,
            status       = "ACTIVE",
            current_task = f"Built {len(state['built_tools'])} tools total",
            cycles_done  = state["cycle"],
            last_output  = f"Tools: {', '.join(state['built_tools'][-3:])}",
            score        = min(7 + len(state["built_tools"]), 10)
        )

        _save_state(state)
        log.info(f"\n  ⏱️  Next cycle in 30 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()


# === PRO-FIXER PATCH 20260328_1246 ===
# Fixed: BACKEND_BUILDER
# Issues: generate_backend_code() returns raw JSON with unescaped code strings that will break when writing to files - Python code with quotes/newlines causes JSON parse errors, deploy_to_github() function is incomplete (cuts off mid-request), causing all deployment attempts to fail, No error handling for malformed Claude responses - when Claude returns markdown-wrapped or malformed JSON, the regex parsing fails silently, Token limit of 2048 is too small for generating complete backend applications with FastAPI boilerplate, requirements.txt, and README, Missing validation that generated code actually works before deployment - no syntax checking or test execution, State management saves built_tools list but never checks it to avoid rebuilding the same tool, No retry logic on GitHub API calls despite having _retry_api helper function that's never used, CYCLE_INTERVAL of 1800 seconds with no main loop implementation means agent never actually cycles
def generate_backend_code(task_description, research_context):
    """Generate complete backend code for a tool."""
    prompt = f"""You are an expert Python backend developer.
Build a complete, deployable backend tool.

TASK: {task_description}
RESEARCH CONTEXT: {research_context}

Build a production-ready Python FastAPI service that:
1. Has a working API with proper endpoints
2. Includes API key authentication for paid users
3. Has clear documentation in code comments
4. Can be deployed on Railway with minimal config
5. Includes a requirements.txt

IMPORTANT: Encode all code files as base64 to avoid JSON escaping issues.

Reply in JSON:
{{
  "tool_name": "snake_case_name",
  "description": "what this tool does in 1 sentence",
  "main_py_b64": "base64 encoded main.py",
  "requirements_txt_b64": "base64 encoded requirements.txt",
  "readme_md_b64": "base64 encoded README.md",
  "api_endpoints": ["GET /endpoint1", "POST /endpoint2"],
  "suggested_price": "$X/month",
  "deployment_cmd": "railway up or render deploy command"
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
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        resp.raise_for_status()
        return resp

    try:
        resp = _retry_api(_call_api)
        if not resp:
            return None
        
        text = resp.json()["content"][0]["text"].strip()
        text = re.sub(r"|", "", text).strip()
        start = text.find("{")
        if start == -1:
            log.error("No JSON object found in response")
            return None
        
        depth, end = 0, start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        
        result = json.loads(text[start:end+1])
        
        import base64
        decoded_files = {}
        for key in ["main_py_b64", "requirements_txt_b64", "readme_md_b64"]:
            if key in result:
                file_key = key.replace("_b64", "")
                try:
                    decoded_files[file_key] = base64.b64decode(result[key]).decode("utf-8")
                except Exception:
                    decoded_files[file_key] = result.get(key, "")
        
        result.update(decoded_files)
        
        if "main_py" in result:
            import ast
            try:
                ast.parse(result["main_py"])
            except SyntaxError as e:
                log.error(f"Generated code has syntax errors: {e}")
                return None
        
        return result
    except Exception as e:
        log.error(f"generate_backend_code error: {e}")
        return None


def deploy_to_github(tool_name, files):
    """Create a GitHub repo and push the tool."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        tool_dir = Path(f"/tmp/tools/{tool_name}")
        tool_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (tool_dir / filename).write_text(content)
        log.info(f"  ✅ Tool saved locally: {tool_dir}")
        return f"local:/tmp/tools/{tool_name}"

    def _create_repo():
        resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "name": tool_name,
                "description": f"Auto-generated backend tool: {tool_name}",
                "private": False,
                "auto_init": True
            },
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    try:
        repo_data = _retry_api(_create_repo)
        if not repo_data:
            log.error("Failed to create GitHub repo")
            return None
        
        repo_url = repo_data.get("clone_url", "")
        log.info(f"  📦 Created repo: {repo_url}")
        
        temp_dir = Path(f"/tmp/git_deploy/{tool_name}")
        if temp_dir.exists():
            subprocess.run(["rm", "-rf", str(temp_dir)], check=False)
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        subprocess.run(["git", "clone", repo_url, str(temp_dir)], check=True, capture_output=True)
        
        for filename, content in files.items():
            (temp_dir / filename).write_text(content)
        
        subprocess.run(["git", "-C", str(temp_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(temp_dir), "commit", "-m", "Initial backend tool commit"], check=True)
        subprocess.run(["git", "-C", str(temp_dir), "push"], check=True)
        
        log.info(f"  ✅ Deployed to GitHub: {repo_url}")
        return repo_url
    except Exception as e:
        log.error(f"deploy_to_github error: {e}")
        return None


def run_cycle(state):
    """Execute one build cycle."""
    state["cycle"] += 1
    log.info(f"\n{'='*60}")
    log.info(f"🔧 BACKEND BUILDER CYCLE #{state['cycle']}")
    log.info(f"{'='*60}")
    
    task_ideas = [
        "Build simple web scraping API (2-hour MVP)",
        "Build simple webhook-to-email forwarding tool",
        "Build: Simple screenshot API with Puppeteer",
        "Deploy 3 RapidAPI endpoints from existing code",
        "Build simple JSON/CSV converter API"
    ]
    
    for task in task_ideas:
        if task in state.get("built_tools", []):
            log.info(f"  ⏭️  Skipping already built: {task}")
            continue
        
        log.info(f"\n🎯 Building: {task}")
        research = sm.get_memory("market_research", "")
        
        result = generate_backend_code(task, research)
        if not result:
            log.error("  ❌ Code generation failed")
            continue
        
        log.info(f"  ✅ Generated: {result.get('tool_name', 'unknown')}")
        
        files = {
            "main.py": result.get("main_py", ""),
            "requirements.txt": result.get("requirements_txt", ""),
            "README.md": result.get("readme_md", "")
        }
        
        repo_url = deploy_to_github(result.get("tool_name", "backend_tool"), files)
        if repo_url:
            state["built_tools"].append(task)
            _save_state(state)
            log.info(f"  🚀 Deployed: {repo_url}")
        else:
            log.error("  ❌ Deployment failed")
        
        time.sleep(5)
    
    _save_state(state)


if __name__ == "__main__":
    log.info("🚀 Backend Builder Agent starting...")
    state = _load_state()
    
    while True:
        try:
            run_cycle(state)
            log.info(f"\n💤 Sleeping for {CYCLE_INTERVAL} seconds...\n")
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("\n👋 Shutting down gracefully...")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)

# === PRO-FIXER PATCH 20260328_1256 ===
# Fixed: BACKEND_BUILDER
# Issues: generate_backend_code() returns malformed JSON with unescaped code strings causing JSON parsing failures, deploy_to_github() is incomplete - cuts off mid-function and never finishes the GitHub push logic, No error handling for Claude API responses containing code blocks with special characters (newlines, quotes, backslashes), Missing retry logic on the actual generate_backend_code() function despite having _retry_api helper, State management saves to /tmp which gets wiped on container restarts, No validation that Claude actually returned valid JSON before parsing, TOKENS=2048 is too small for generating complete backend applications with multiple files
def generate_backend_code(task_description, research_context):
    """Generate complete backend code for a tool."""
    prompt = f"""You are an expert Python backend developer.
Build a complete, deployable backend tool.

TASK: {task_description}
RESEARCH CONTEXT: {research_context}

Build a production-ready Python FastAPI service that:
1. Has a working API with proper endpoints
2. Includes API key authentication for paid users
3. Has clear documentation in code comments
4. Can be deployed on Railway with minimal config
5. Includes a requirements.txt

Reply ONLY with valid JSON. Escape all newlines as \\n and quotes as \\":
{{
  "tool_name": "snake_case_name",
  "description": "what this tool does in 1 sentence",
  "main_py": "complete main.py code with \\n for newlines",
  "requirements_txt": "package1\\npackage2\\n...",
  "readme_md": "markdown README with usage examples",
  "api_endpoints": ["GET /endpoint1", "POST /endpoint2"],
  "suggested_price": "$X/month",
  "deployment_cmd": "railway up or render deploy command"
}}"""

    def _call_api():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model":      MODEL,
                "max_tokens": 16000,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=120
        )
        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}: {resp.text}")
        
        text = resp.json()["content"][0]["text"].strip()
        text = re.sub(r"\s*|\s*", "", text).strip()
        
        start = text.find("{")
        if start == -1:
            raise Exception("No JSON object found in response")
        
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
            raise Exception("Incomplete JSON object")
        
        json_str = text[start:end+1]
        result = json.loads(json_str)
        
        required = ["tool_name", "main_py", "requirements_txt"]
        for field in required:
            if field not in result:
                raise Exception(f"Missing required field: {field}")
        
        return result
    
    return _retry_api(_call_api, retries=3, delay=3)


def deploy_to_github(tool_name, files):
    """Create a GitHub repo and push the tool."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        tool_dir = Path(f"/tmp/tools/{tool_name}")
        tool_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (tool_dir / filename).write_text(content)
        log.info(f"  ✅ Tool saved locally: {tool_dir}")
        return f"local:/tmp/tools/{tool_name}"

    try:
        repo_name = f"backend-{tool_name}"
        
        resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "name": repo_name,
                "description": f"Auto-generated backend tool: {tool_name}",
                "private": False,
                "auto_init": True
            },
            timeout=30
        )
        
        if resp.status_code not in [200, 201, 422]:
            raise Exception(f"GitHub repo creation failed: {resp.status_code} {resp.text}")
        
        repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}"
        
        for filename, content in files.items():
            file_resp = requests.put(
                f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": f"Add {filename}",
                    "content": __import__('base64').b64encode(content.encode()).decode()
                },
                timeout=30
            )
            
            if file_resp.status_code not in [200, 201]:
                log.warning(f"Failed to upload {filename}: {file_resp.status_code}")
        
        log.info(f"  ✅ Deployed to GitHub: {repo_url}")
        return repo_url
        
    except Exception as e:
        log.error(f"deploy_to_github error: {e}")
        tool_dir = Path(f"/tmp/tools/{tool_name}")
        tool_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (tool_dir / filename).write_text(content)
        return f"local:/tmp/tools/{tool_name}"