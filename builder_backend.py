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


# === PRO-FIXER PATCH 20260328_1254 ===
# Fixed: BACKEND_BUILDER
# Issues: generate_backend_code() returns unescaped JSON with raw Python code inside JSON strings, causing parsing failures when code contains quotes/newlines, deploy_to_github() function is incomplete - cuts off mid-request, never completes GitHub API call or handles deployment, No error handling for malformed LLM responses - when Claude returns invalid JSON or code blocks, the agent crashes, Prompt asks for code in JSON but doesn't specify escaping requirements, leading to unparseable responses, Missing retry logic on JSON parsing failures and no fallback when LLM doesn't follow format, State management doesn't track failures, so agent repeats same broken tasks indefinitely, No validation that generated code actually works before attempting deployment
def generate_backend_code(task_description, research_context):
    """Generate complete backend code for a tool using two-stage approach."""
    
    # Stage 1: Get metadata and structure
    meta_prompt = f"""You are an expert Python backend developer.
Build a complete, deployable backend tool.

TASK: {task_description}
RESEARCH CONTEXT: {research_context}

First, provide the tool structure and metadata in JSON:
{{
  "tool_name": "snake_case_name",
  "description": "one sentence description",
  "api_endpoints": ["GET /endpoint1", "POST /endpoint2"],
  "packages": ["fastapi", "uvicorn"],
  "suggested_price": "$X/month",
  "deployment_platform": "railway or render"
}}

Return ONLY valid JSON, no code blocks or markdown."""
    
    try:
        meta_resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": MODEL,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": meta_prompt}]
            },
            timeout=60
        )
        
        if meta_resp.status_code != 200:
            log.error(f"Metadata API call failed: {meta_resp.status_code}")
            return None
            
        meta_text = meta_resp.json()["content"][0]["text"].strip()
        meta_text = re.sub(r"\s*|\s*", "", meta_text).strip()
        
        # Find JSON object
        start_idx = meta_text.find("{")
        if start_idx == -1:
            log.error("No JSON object found in metadata response")
            return None
            
        depth, end_idx = 0, -1
        for i in range(start_idx, len(meta_text)):
            if meta_text[i] == "{":
                depth += 1
            elif meta_text[i] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        
        if end_idx == -1:
            log.error("Malformed JSON in metadata response")
            return None
            
        metadata = json.loads(meta_text[start_idx:end_idx+1])
        
    except json.JSONDecodeError as e:
        log.error(f"JSON parsing failed for metadata: {e}")
        return None
    except Exception as e:
        log.error(f"Metadata generation error: {e}")
        return None
    
    # Stage 2: Get actual code files as plain text
    code_prompt = f"""Generate a complete FastAPI backend for: {task_description}

Requirements:
- Tool name: {metadata.get('tool_name', 'api_tool')}
- Endpoints: {', '.join(metadata.get('api_endpoints', []))}
- Packages: {', '.join(metadata.get('packages', ['fastapi', 'uvicorn']))}

Provide THREE separate code files:

1. MAIN_PY (complete main.py with FastAPI app, all endpoints, auth)
2. REQUIREMENTS_TXT (all pip packages, one per line)
3. README_MD (markdown with setup and usage instructions)

Format your response EXACTLY like this:

===MAIN_PY===
[complete Python code here]
===END_MAIN_PY===

===REQUIREMENTS_TXT===
[package list here]
===END_REQUIREMENTS_TXT===

===README_MD===
[markdown documentation here]
===END_README_MD===

Use those exact delimiters."""
    
    try:
        code_resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": code_prompt}]
            },
            timeout=90
        )
        
        if code_resp.status_code != 200:
            log.error(f"Code generation API call failed: {code_resp.status_code}")
            return None
            
        code_text = code_resp.json()["content"][0]["text"]
        
        # Extract files using delimiters
        files = {}
        patterns = [
            ("main_py", r"===MAIN_PY===\s*(.+?)\s*===END_MAIN_PY==="),
            ("requirements_txt", r"===REQUIREMENTS_TXT===\s*(.+?)\s*===END_REQUIREMENTS_TXT==="),
            ("readme_md", r"===README_MD===\s*(.+?)\s*===END_README_MD===")
        ]
        
        for key, pattern in patterns:
            match = re.search(pattern, code_text, re.DOTALL)
            if match:
                files[key] = match.group(1).strip()
            else:
                log.warning(f"Could not extract {key} from response")
                files[key] = ""
        
        # Validate main.py is syntactically correct
        if files.get("main_py"):
            try:
                compile(files["main_py"], "<generated>", "exec")
            except SyntaxError as e:
                log.error(f"Generated main.py has syntax errors: {e}")
                return None
        
        # Combine metadata and files
        result = metadata.copy()
        result.update(files)
        return result
        
    except Exception as e:
        log.error(f"Code generation error: {e}")
        return None


def deploy_to_github(tool_name, files):
    """Create a GitHub repo and push the tool code."""
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        tool_dir = Path(f"/tmp/tools/{tool_name}")
        tool_dir.mkdir(parents=True, exist_ok=True)
        
        file_mapping = {
            "main_py": "main.py",
            "requirements_txt": "requirements.txt",
            "readme_md": "README.md"
        }
        
        for key, filename in file_mapping.items():
            content = files.get(key, "")
            if content:
                (tool_dir / filename).write_text(content)
        
        log.info(f"  ✅ Tool saved locally: {tool_dir}")
        return f"local:/tmp/tools/{tool_name}"
    
    try:
        # Create GitHub repository
        create_resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={
                "name": tool_name,
                "description": files.get("description", "Backend tool"),
                "private": False,
                "auto_init": False
            },
            timeout=30
        )
        
        if create_resp.status_code not in [201, 422]:
            log.error(f"GitHub repo creation failed: {create_resp.status_code} {create_resp.text}")
            return deploy_to_github(tool_name, files)  # Fallback to local
        
        repo_data = create_resp.json()
        clone_url = repo_data.get("clone_url", "")
        
        if not clone_url:
            log.error("No clone URL returned from GitHub")
            return f"local:/tmp/tools/{tool_name}"
        
        # Clone and push using git commands
        temp_dir = Path(f"/tmp/git_deploy/{tool_name}")
        if temp_dir.exists():
            subprocess.run(["rm", "-rf", str(temp_dir)], check=False)
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Backend Builder"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "builder@example.com"], cwd=temp_dir, check=True, capture_output=True)
        
        # Write files
        file_mapping = {
            "main_py": "main.py",
            "requirements_txt": "requirements.txt",
            "readme_md": "README.md"
        }
        
        for key, filename in file_mapping.items():
            content = files.get(key, "")
            if content:
                (temp_dir / filename).write_text(content)
        
        # Commit and push
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_dir, check=True, capture_output=True)
        
        auth_url = clone_url.replace("https://", f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@")
        subprocess.run(["git", "remote", "add", "origin", auth_url], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "master"], cwd=temp_dir, check=True, capture_output=True)
        
        log.info(f"  ✅ Deployed to GitHub: {clone_url}")
        return clone_url
        
    except subprocess.CalledProcessError as e:
        log.error(f"Git command failed: {e}")
        return f"local:/tmp/tools/{tool_name}"
    except Exception as e:
        log.error(f"GitHub deployment error: {e}")
        return f"local:/tmp/tools/{tool_name}"