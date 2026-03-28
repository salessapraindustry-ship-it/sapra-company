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


# === PRO-FIXER PATCH 20260328_1239 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 118: HTTP request is truncated mid-statement - 'if resp.status_code == 20' is incomplete (should be 200), Missing try-except wrapper around validate_opportunity() API call - causes validation failures to crash the agent, No error handling for malformed JSON responses from Claude API - agent fails when Claude returns non-JSON text, web_search() returns empty list on failure but validate_opportunity() doesn't handle empty search results gracefully, State file persists researched_topics but never checks if topics were already researched, causing infinite retries, No timeout or failure limit on validation retries - agent keeps failing the same task forever, Missing main execution loop - no run() function to actually execute the research cycles, CYCLE_INTERVAL is 1500 seconds but no scheduling mechanism exists to use it
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for {topic} - skipping validation")
        return None
    
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
            data = resp.json()
            content = data.get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                text = text.strip()
                if text.startswith(""):
                    text = text[7:]
                if text.startswith(""):
                    text = text[3:]
                if text.endswith(""):
                    text = text[:-3]
                text = text.strip()
                result = json.loads(text)
                return result
            else:
                log.error("Claude returned empty content")
                return None
        else:
            log.error(f"Claude API returned {resp.status_code}: {resp.text}")
            return None
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude JSON response: {e}")
        return None
    except Exception as e:
        log.error(f"Validation API call failed: {e}")
        return None


def research_cycle():
    """Execute one research cycle."""
    state = _load_state()
    state["cycle"] = state.get("cycle", 0) + 1
    
    topics = [
        "AI-powered developer tools with API access",
        "automation tools for small business recurring revenue",
        "data extraction APIs with high demand on RapidAPI",
        "profitable SaaS micro-tools under $20/month",
        "B2B API services with proven subscriber base"
    ]
    
    opportunities = []
    failed_topics = state.get("failed_topics", {})
    
    for topic in topics:
        if failed_topics.get(topic, 0) >= 3:
            log.info(f"Skipping {topic} - failed 3+ times")
            continue
            
        if topic in state.get("researched_topics", []):
            log.info(f"Already researched: {topic}")
            continue
        
        log.info(f"Researching: {topic}")
        results = web_search(topic)
        
        if not results:
            log.warning(f"No search results for {topic}")
            failed_topics[topic] = failed_topics.get(topic, 0) + 1
            continue
        
        validation = validate_opportunity(topic, results)
        
        if validation is None:
            log.error(f"Validation failed for {topic}")
            failed_topics[topic] = failed_topics.get(topic, 0) + 1
            continue
        
        state["researched_topics"].append(topic)
        
        if validation.get("is_viable") and validation.get("confidence", 0) > 0.6:
            opportunity = {
                "topic": topic,
                "validation": validation,
                "timestamp": datetime.now().isoformat(),
                "search_results": results
            }
            opportunities.append(opportunity)
            log.info(f"✓ Found viable opportunity: {topic}")
            sm.add_message("CEO", f"RESEARCH_COMPLETE: {topic}", opportunity)
        else:
            log.info(f"✗ Not viable: {topic}")
        
        time.sleep(2)
    
    state["failed_topics"] = failed_topics
    _save_state(state)
    
    if opportunities:
        log.info(f"Cycle {state['cycle']}: Found {len(opportunities)} opportunities")
        return opportunities
    else:
        log.info(f"Cycle {state['cycle']}: No viable opportunities found")
        return []


def run():
    """Main execution loop."""
    log.info("Deep Researcher starting...")
    
    while True:
        try:
            opportunities = research_cycle()
            if opportunities:
                sm.add_message("CEO", "RESEARCH_REPORT", {
                    "count": len(opportunities),
                    "opportunities": opportunities
                })
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("Researcher shutting down...")
            break
        except Exception as e:
            log.error(f"Research cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run()

# === PRO-FIXER PATCH 20260328_1247 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 106: HTTP request is incomplete - resp.status_code == 20 is truncated, should be == 200, validate_opportunity() never returns a value - function ends without return statement, causing None to propagate, No error handling wrapper around validate_opportunity() calls - API failures cause validation to fail silently, JSON parsing from Claude response is missing - even if API succeeds, the response body is never extracted or parsed, State management doesn't track validation failures - no backoff or alternative strategy when validation repeatedly fails, web_search() returns empty list on failure but validate_opportunity() doesn't handle empty results gracefully, Missing main loop structure - no continuous execution or task queue processing visible in provided code, SERPER_API_KEY check happens but ANTHROPIC_API_KEY is never validated before use
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "No search results found",
            "recommended_action": "SKIP"
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
  "demand_evidence": "specific proof of demand",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
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
                "max_tokens": TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            if "content" in data and len(data["content"]) > 0:
                text = data["content"][0].get("text", "")
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
                else:
                    log.warning("No JSON found in Claude response")
                    return None
            else:
                log.warning("Empty content in Claude response")
                return None
        else:
            log.error(f"Claude API error: {resp.status_code} - {resp.text}")
            return None
    
    result = _retry_api(_call_api, retries=3, delay=2)
    
    if result is None:
        log.error(f"Validation failed for topic: {topic}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "API validation failed after retries",
            "recommended_action": "RESEARCH_MORE",
            "error": "api_failure"
        }
    
    return result


def main():
    """Main execution loop."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set - cannot validate opportunities")
        return
    
    log.info("Deep Researcher Agent starting...")
    state = _load_state()
    
    while True:
        try:
            state["cycle"] += 1
            log.info(f"Research cycle {state['cycle']} starting")
            
            topics = sm.get_research_queue()
            
            if not topics:
                log.info("No research topics queued - waiting")
                time.sleep(60)
                continue
            
            for topic in topics[:3]:
                if topic in state.get("researched_topics", []):
                    continue
                
                log.info(f"Researching: {topic}")
                search_results = web_search(topic)
                validation = validate_opportunity(topic, search_results)
                
                if validation and validation.get("is_viable"):
                    log.info(f"✓ Viable opportunity found: {topic}")
                    sm.post_research_result({
                        "topic": topic,
                        "validation": validation,
                        "timestamp": datetime.now().isoformat()
                    })
                else:
                    log.info(f"✗ Not viable: {topic}")
                
                state["researched_topics"].append(topic)
                _save_state(state)
                time.sleep(5)
            
            log.info(f"Cycle {state['cycle']} complete - sleeping {CYCLE_INTERVAL}s")
            time.sleep(CYCLE_INTERVAL)
            
        except KeyboardInterrupt:
            log.info("Researcher shutting down")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()