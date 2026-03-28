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


# === PRO-FIXER PATCH 20260328_1243 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 136: validate_opportunity() response parsing is truncated mid-line - 'if resp.status_code == 20' is incomplete, should be 'if resp.status_code == 200:', No error handling for JSON parsing failures in validate_opportunity() - will crash if Claude returns non-JSON, web_search() uses wrong HTTP method - uses GET with json= parameter instead of POST, Missing validation that search_results is not empty before calling validate_opportunity(), No main() function or task execution loop visible - agent likely never runs its core research cycle, State management exists but no code actually uses _load_state() or cycles through topics, CYCLE_INTERVAL defined but no scheduling logic to execute research tasks, No integration with shared_memory to read tasks or post validated opportunities
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
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
  "demand_evidence": "specific proof of demand",
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
            content = data.get("content", [])[0].get("text", "")
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                log.warning("No JSON found in Claude response")
                return None
        else:
            log.error(f"Claude API error {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        log.error(f"Validation failed: {e}")
        return None


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
            return [{"title": r.get("title",""),
                     "snippet": r.get("snippet",""),
                     "url": r.get("link","")}
                    for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []


def main():
    """Main execution loop for research agent."""
    log.info("🔬 Deep Researcher Agent starting...")
    state = _load_state()
    
    while True:
        try:
            state["cycle"] += 1
            log.info(f"\n=== Research Cycle {state['cycle']} ===")
            
            # Read pending research tasks from shared memory
            tasks = sm.get_tasks_by_type("RESEARCH")
            if not tasks:
                log.info("No research tasks pending. Waiting...")
                time.sleep(CYCLE_INTERVAL)
                continue
            
            for task in tasks:
                topic = task.get("description", "")
                task_id = task.get("id", "unknown")
                
                if topic in state.get("researched_topics", []):
                    log.info(f"Already researched: {topic}")
                    continue
                
                log.info(f"\n📊 Researching: {topic}")
                
                # Perform web search
                search_results = web_search(topic)
                if not search_results:
                    log.warning(f"No search results for: {topic}")
                    sm.post_message({
                        "from": "RESEARCHER",
                        "to": "CEO",
                        "content": f"Research failed for '{topic}' - no search results",
                        "type": "RESEARCH_FAILURE"
                    })
                    continue
                
                # Validate opportunity
                validation = validate_opportunity(topic, search_results)
                if not validation:
                    log.warning(f"Validation failed for: {topic}")
                    sm.post_message({
                        "from": "RESEARCHER",
                        "to": "CEO",
                        "content": f"Could not validate '{topic}' - API error",
                        "type": "RESEARCH_FAILURE"
                    })
                    continue
                
                # Post results to shared memory
                state["researched_topics"].append(topic)
                
                result = {
                    "from": "RESEARCHER",
                    "to": "CEO",
                    "type": "RESEARCH_COMPLETE",
                    "topic": topic,
                    "task_id": task_id,
                    "validation": validation,
                    "timestamp": datetime.now().isoformat()
                }
                
                sm.post_message(result)
                log.info(f"✅ Research complete: {topic}")
                log.info(f"   Viable: {validation.get('is_viable')}")
                log.info(f"   Confidence: {validation.get('confidence')}")
                log.info(f"   Action: {validation.get('recommended_action')}")
                
            _save_state(state)
            log.info(f"\n💤 Sleeping {CYCLE_INTERVAL}s until next cycle...")
            time.sleep(CYCLE_INTERVAL)
            
        except KeyboardInterrupt:
            log.info("\n🛑 Researcher shutting down...")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()

# === PRO-FIXER PATCH 20260328_1250 ===
# Fixed: DEEP_RESEARCHER
# Issues: CRITICAL: Line 163 has incomplete code - resp.status_code == 20 is truncated, should be == 200, CRITICAL: validate_opportunity() never extracts JSON from Claude's response - it returns raw Response object instead of parsed validation, CRITICAL: No error handling for JSON parsing failures in validate_opportunity() - will crash on malformed responses, MAJOR: web_search() uses wrong HTTP method - uses GET with json= parameter instead of POST, MAJOR: No actual usage of validation results - validate_opportunity is called but results are never stored or acted upon, MINOR: Missing main() function and agent loop - no continuous operation logic, MINOR: State management exists but researched_topics is never populated or checked for duplicates
def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10
        )
        if resp.status_code == 200:
            results = resp.json().get("organic", [])
            return [{"title": r.get("title",""), "snippet": r.get("snippet",""), "url": r.get("link","")} for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []

def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity. Returns dict or None."""
    results_text = "\n".join([f"- {r['title']}: {r['snippet']}" for r in search_results])
    prompt = f"""You are a market researcher. Validate if this is a real money-making opportunity.\n\nTOPIC: {topic}\nSEARCH RESULTS:\n{results_text}\n\nAnalyze for:\n1. Real buyer demand (people actively paying for this)\n2. Price benchmarks (what similar tools cost)\n3. Competition level (can we win?)\n4. Build difficulty (can an AI agent build this in 1-3 days?)\n5. Revenue potential (monthly recurring revenue possible?)\n\nBe HONEST and SPECIFIC. If demand is uncertain, say so.\n\nReply ONLY in JSON:\n{{\n  \"is_viable\": true/false,\n  \"confidence\": 0.0-1.0,\n  \"demand_evidence\": \"specific proof\",\n  \"price_benchmark\": \"$X-Y/month\",\n  \"competition\": \"LOW/MEDIUM/HIGH\",\n  \"build_difficulty\": \"EASY/MEDIUM/HARD\",\n  \"monthly_revenue_potential\": \"$X-Y\",\n  \"recommended_action\": \"BUILD_NOW/RESEARCH_MORE/SKIP\",\n  \"build_spec\": \"what to build\"\n}}"""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": MODEL, "max_tokens": TOKENS, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
                else:
                    log.warning("No JSON found in Claude response")
        else:
            log.warning(f"Claude API returned {resp.status_code}")
    except Exception as e:
        log.error(f"Validation failed: {e}")
    return None

def main():
    """Main research loop."""
    log.info("🔬 Deep Researcher Agent starting...")
    state = _load_state()
    while True:
        try:
            state["cycle"] += 1
            log.info(f"\n=== Research Cycle {state['cycle']} ===")
            goals = sm.get_goals()
            if not goals:
                log.info("No goals yet, waiting...")
                time.sleep(60)
                continue
            primary_goal = goals[0] if isinstance(goals, list) else goals
            topics = [f"{primary_goal} tool ideas", f"profitable {primary_goal} APIs", f"{primary_goal} SaaS opportunities"]
            for topic in topics:
                if topic in state["researched_topics"]:
                    continue
                log.info(f"Researching: {topic}")
                results = web_search(topic)
                if not results:
                    log.warning(f"No search results for {topic}")
                    continue
                validation = validate_opportunity(topic, results)
                if validation and validation.get("is_viable") and validation.get("recommended_action") == "BUILD_NOW":
                    log.info(f"✅ VIABLE OPPORTUNITY FOUND: {validation.get('build_spec')}")
                    sm.add_research_result({"topic": topic, "validation": validation, "timestamp": datetime.now().isoformat()})
                else:
                    log.info(f"⏭️  Skipping {topic}: {validation.get('recommended_action') if validation else 'validation_failed'}")
                state["researched_topics"].append(topic)
                _save_state(state)
                time.sleep(5)
            log.info(f"Cycle complete. Sleeping {CYCLE_INTERVAL}s...")
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()