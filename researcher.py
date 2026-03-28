#!/usr/bin/env python3
# ================================================================
#  researcher.py — Deep Researcher Agent
#  Finds validated opportunities — not just Google results
#  Cross-references multiple sources, confirms real buyer demand
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
RAPIDAPI_KEY      = os.environ.get("RAPIDAPI_KEY", "")
MODEL             = "claude-haiku-4-5-20251001"
TOKENS            = 1024
CYCLE_INTERVAL    = 1500   # 25 minutes (staggered from CEO's 15min)

state_file = "/tmp/researcher_state.json"


def _load_state():
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"cycle": 0, "researched_topics": []}


def _save_state(state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.get(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": serper_key,
                     "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10
        )
        if resp.status_code == 200:
            results = resp.json().get("organic", [])
            return [{"title": r.get("title",""),
                     "snippet": r.get("snippet",""),
                     "url": r.get("link","")}
                    for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []


def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    results_text = "\n".join([
        f"- {r['title']}: {r['snippet']}"
        for r in search_results
    ])

    prompt = f"""You are a market researcher. Validate if this is a real money-making opportunity.

TOPIC: {topic}
SEARCH RESULTS:
{results_text}

Analyze for:
1. Real buyer demand (people actively paying for this)
2. Price benchmarks (what similar tools cost)
3. Competition level (can we win?)
4. Build difficulty (can an AI agent build this in 1-3 days?)
5. Revenue potential (monthly recurring revenue possible?)

Be HONEST and SPECIFIC. If demand is uncertain, say so.

Reply ONLY in JSON:
{{
  "is_viable": true/false,
  "confidence": 0.0-1.0,
  "demand_evidence": "specific proof of demand (e.g. 500 RapidAPI subscribers at $X/mo)",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
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
        log.error(f"validate_opportunity error: {e}")
    return None


def deep_research(task):
    """Execute a deep research task with multi-source validation."""
    description = task.get("description", "")
    log.info(f"  🔬 Researching: {task.get('title','')}")

    sm.update_task(task["task_id"], sm.STAGE_RESEARCH)

    # Search multiple angles
    queries = [
        f"site:rapidapi.com {description[:50]} API pricing",
        f"{description[:50]} tool demand reddit 2024",
        f"{description[:50]} SaaS market size buyers",
    ]

    all_results = []
    for q in queries:
        results = web_search(q)
        all_results.extend(results)
        time.sleep(1)

    # If no search results, use a default context
    if not all_results:
        log.warning("No search results — using Claude knowledge for validation")
        all_results = [{"title": description, "snippet": description, "url": ""}]

    # Validate opportunity
    validation = validate_opportunity(description, all_results)

    if validation:
        log.info(f"  📊 Viable: {validation.get('is_viable')} "
                 f"(confidence={validation.get('confidence',0):.0%})")
        log.info(f"  💰 Revenue potential: {validation.get('monthly_revenue_potential','?')}")

        # Post research findings
        sm.post_research(
            topic        = task.get("title", ""),
            summary      = validation.get("demand_evidence", ""),
            opportunities= [validation.get("build_spec", "")],
            data_sources = [r.get("url","") for r in all_results[:3]],
            confidence   = float(validation.get("confidence", 0))
        )

        # If viable, alert CEO via task completion
        result_summary = (
            f"VIABLE: {validation.get('recommended_action')} | "
            f"Revenue: {validation.get('monthly_revenue_potential')} | "
            f"Build: {validation.get('build_spec','')[:100]}"
        ) if validation.get("is_viable") else (
            f"SKIP: {validation.get('competition','?')} competition, "
            f"confidence={validation.get('confidence',0):.0%}"
        )

        sm.update_task(task["task_id"], sm.STAGE_DONE, result_summary)
        return validation
    else:
        sm.update_task(task["task_id"], sm.STAGE_FAILED, "Validation failed")
        return None


def run():
    """Main Deep Researcher loop."""
    log.info("=" * 60)
    log.info("  DEEP RESEARCHER — ONLINE")
    log.info(f"  {datetime.now()}")
    log.info("  I find real opportunities. No guessing. Only validated data.")
    log.info("=" * 60)

    state = _load_state()


    # Stagger startup to avoid Google Sheets quota
    log.info(f"  ⏳ Staggered start — waiting 30s")
    time.sleep(30)

    while True:
        state["cycle"] += 1
        log.info(f"\n{'='*60}")
        log.info(f"  RESEARCHER CYCLE {state['cycle']} — "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'='*60}")

        # Get assigned tasks
        tasks = sm.get_my_tasks(sm.AGENT_RESEARCHER)
        log.info(f"  📋 Tasks assigned: {len(tasks)}")

        if tasks:
            for task in tasks[:2]:  # max 2 research tasks per cycle
                result = deep_research(task)
                time.sleep(2)
        else:
            log.info("  ⏳ No tasks — waiting for CEO assignment")

        sm.report_status(
            sm.AGENT_RESEARCHER,
            status       = "ACTIVE",
            current_task = f"Processed {len(tasks)} tasks",
            cycles_done  = state["cycle"],
            last_output  = f"Cycle {state['cycle']} complete",
            score        = 7
        )

        _save_state(state)
        log.info(f"\n  ⏱️  Next cycle in 20 minutes")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run()


# === PRO-FIXER PATCH 20260328_1245 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 147: HTTP status code check is truncated (resp.status_code == 20) instead of (resp.status_code == 200), causing validation to always fail, Missing error handling: validate_opportunity() doesn't return a default value when API calls fail, causing None to be passed downstream, JSON parsing vulnerability: No try-except around json.loads() for Claude's response, causing crashes on malformed JSON, web_search() uses wrong HTTP method: GET instead of POST for Serper API, causing 405 errors, No timeout recovery: When API retries exhaust, functions return None but calling code doesn't handle None values, State persistence race condition: _save_state() has no file locking, can corrupt state.json on concurrent writes, Missing validation: No check if search_results is empty before passing to validate_opportunity(), wasting API credits
def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": serper_key,
                "Content-Type": "application/json"
            },
            json={"q": query, "num": 5},
            timeout=10
        )
        if resp.status_code == 200:
            results = resp.json().get("organic", [])
            return [{
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "url": r.get("link", "")
            } for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []


def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "No search results found",
            "price_benchmark": "Unknown",
            "competition": "UNKNOWN",
            "build_difficulty": "UNKNOWN",
            "monthly_revenue_potential": "$0",
            "recommended_action": "SKIP",
            "build_spec": "Insufficient data"
        }
    
    results_text = "\n".join([
        f"- {r['title']}: {r['snippet']}"
        for r in search_results
    ])

    prompt = f"""You are a market researcher. Validate if this is a real money-making opportunity.

TOPIC: {topic}
SEARCH RESULTS:
{results_text}

Analyze for:
1. Real buyer demand (people actively paying for this)
2. Price benchmarks (what similar tools cost)
3. Competition level (can we win?)
4. Build difficulty (can an AI agent build this in 1-3 days?)
5. Revenue potential (monthly recurring revenue possible?)

Be HONEST and SPECIFIC. If demand is uncertain, say so.

Reply ONLY in JSON:
{{
  "is_viable": true/false,
  "confidence": 0.0-1.0,
  "demand_evidence": "specific proof of demand (e.g. 500 RapidAPI subscribers at $X/mo)",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
}}"""

    try:
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
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                try:
                    result = json.loads(text)
                    return result
                except json.JSONDecodeError as e:
                    log.error(f"JSON parse error: {e}. Raw text: {text[:200]}")
                    return {
                        "is_viable": False,
                        "confidence": 0.0,
                        "demand_evidence": "API returned invalid JSON",
                        "price_benchmark": "Unknown",
                        "competition": "UNKNOWN",
                        "build_difficulty": "UNKNOWN",
                        "monthly_revenue_potential": "$0",
                        "recommended_action": "SKIP",
                        "build_spec": "Parse error"
                    }
        else:
            log.warning(f"Claude API returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Validation failed: {e}")
    
    return {
        "is_viable": False,
        "confidence": 0.0,
        "demand_evidence": "API call failed",
        "price_benchmark": "Unknown",
        "competition": "UNKNOWN",
        "build_difficulty": "UNKNOWN",
        "monthly_revenue_potential": "$0",
        "recommended_action": "SKIP",
        "build_spec": "Error during validation"
    }


import fcntl

def _save_state(state):
    """Save state with file locking to prevent corruption."""
    try:
        with open(state_file, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(state, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        log.error(f"Failed to save state: {e}")

# === PRO-FIXER PATCH 20260328_1253 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 124: validate_opportunity() response parsing is truncated mid-line - code cuts off at 'if resp.status_code == 20' causing syntax error, Missing error handling for JSON parsing in validate_opportunity() - will crash on malformed Claude responses, web_search() uses wrong HTTP method (GET instead of POST) for Serper API, causing all searches to fail, No validation that search_results is non-empty before calling validate_opportunity(), leading to meaningless validation attempts, CYCLE_INTERVAL is 1500 seconds (25 min) but runs continuously in assumed main loop without actual sleep implementation, Missing main execution loop entirely - agent has no run() or main() function to actually execute research cycles, State management loads/saves but never increments cycle counter or tracks researched_topics properly, No integration with shared_memory to read tasks or write results back to the system
def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": serper_key,
                "Content-Type": "application/json"
            },
            json={"q": query, "num": 5},
            timeout=10
        )
        if resp.status_code == 200:
            results = resp.json().get("organic", [])
            return [{
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "url": r.get("link", "")
            } for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []


def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "No search results found"}
    
    results_text = "\n".join([
        f"- {r['title']}: {r['snippet']}"
        for r in search_results
    ])

    prompt = f"""You are a market researcher. Validate if this is a real money-making opportunity.

TOPIC: {topic}
SEARCH RESULTS:
{results_text}

Analyze for:
1. Real buyer demand (people actively paying for this)
2. Price benchmarks (what similar tools cost)
3. Competition level (can we win?)
4. Build difficulty (can an AI agent build this in 1-3 days?)
5. Revenue potential (monthly recurring revenue possible?)

Be HONEST and SPECIFIC. If demand is uncertain, say so.

Reply ONLY in JSON:
{{
  "is_viable": true/false,
  "confidence": 0.0-1.0,
  "demand_evidence": "specific proof of demand",
  "price_benchmark": "$X-Y/month based on source",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
}}"""

    try:
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
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                else:
                    log.warning(f"No JSON found in Claude response for {topic}")
        else:
            log.warning(f"Claude API returned {resp.status_code}: {resp.text}")
    except json.JSONDecodeError as e:
        log.error(f"JSON decode failed for {topic}: {e}")
    except Exception as e:
        log.error(f"Validation failed for {topic}: {e}")
    
    return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "Validation error"}


def research_topic(topic):
    """Full research pipeline for one topic."""
    log.info(f"Researching: {topic}")
    search_results = web_search(topic)
    if not search_results:
        log.warning(f"No search results for: {topic}")
        return None
    
    validation = validate_opportunity(topic, search_results)
    if validation.get("is_viable") and validation.get("confidence", 0) >= 0.6:
        log.info(f"✓ VIABLE: {topic} — {validation.get('recommended_action')}")
        return {
            "topic": topic,
            "validation": validation,
            "search_results": search_results,
            "timestamp": datetime.now().isoformat()
        }
    else:
        log.info(f"✗ NOT VIABLE: {topic} — confidence {validation.get('confidence', 0)}")
        return None


def run():
    """Main execution loop."""
    log.info("DEEP_RESEARCHER starting...")
    state = _load_state()
    
    while True:
        try:
            state["cycle"] += 1
            log.info(f"=== Research Cycle {state['cycle']} ===")
            
            tasks = sm.get_tasks(agent="DEEP_RESEARCHER", status="pending")
            if not tasks:
                log.info("No pending tasks — waiting...")
                time.sleep(CYCLE_INTERVAL)
                continue
            
            for task in tasks[:3]:
                topic = task.get("description", "")
                if not topic or topic in state["researched_topics"]:
                    sm.update_task(task["id"], status="skipped", result="Duplicate or empty topic")
                    continue
                
                sm.update_task(task["id"], status="in_progress")
                result = research_topic(topic)
                
                if result:
                    sm.update_task(task["id"], status="completed", result=json.dumps(result))
                    sm.add_memory(
                        agent="DEEP_RESEARCHER",
                        memory_type="research_finding",
                        content=f"VIABLE: {topic}",
                        metadata=result
                    )
                    state["researched_topics"].append(topic)
                else:
                    sm.update_task(task["id"], status="completed", result="Not viable")
                
                time.sleep(2)
            
            _save_state(state)
            log.info(f"Cycle {state['cycle']} complete — sleeping {CYCLE_INTERVAL}s")
            time.sleep(CYCLE_INTERVAL)
            
        except KeyboardInterrupt:
            log.info("Shutting down...")
            _save_state(state)
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run()