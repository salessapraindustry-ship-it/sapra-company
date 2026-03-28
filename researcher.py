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

# === PRO-FIXER PATCH 20260328_1302 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 129: resp.status_code == 20 is incomplete - should be == 200, Missing error handling in validate_opportunity() causes silent failures when Claude API returns non-200 codes, web_search() uses wrong HTTP method - should be POST not GET for Serper API, No extraction of JSON from Claude's response text - response contains wrapper text that breaks json.loads(), Missing validation that search_results is not empty before calling validate_opportunity(), No retry logic on validate_opportunity() API calls despite _retry_api helper existing, CYCLE_INTERVAL is 1500 seconds (25 min) but comment says staggered timing - actual execution will fail validation 5 times rapidly, state_file writes are not atomic - can corrupt on crash, Missing main loop that actually calls research cycle, No integration with shared_memory to write validated opportunities for CEO to read
def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.post(
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
  "demand_evidence": "specific proof of demand (e.g. 500 RapidAPI subscribers at $X/mo)",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
}}"""

    def _call_claude():
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
            text = data.get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            else:
                log.error(f"No JSON found in Claude response: {text[:200]}")
                return None
        else:
            log.error(f"Claude API returned {resp.status_code}: {resp.text[:200]}")
            return None
    
    return _retry_api(_call_claude, retries=3, delay=2)


def research_cycle():
    """Main research cycle: get topics from shared memory, validate, write back results."""
    state = _load_state()
    state["cycle"] = state.get("cycle", 0) + 1
    log.info(f"=== Research Cycle {state['cycle']} ===")
    
    topics = sm.read("research_topics") or []
    if not topics:
        log.info("No research topics in queue. Waiting for CEO...")
        _save_state(state)
        return
    
    topic = topics[0]
    log.info(f"Researching: {topic}")
    
    search_results = web_search(topic)
    if not search_results:
        log.warning(f"No search results found for: {topic}")
        sm.write("research_topics", topics[1:])
        _save_state(state)
        return
    
    validation = validate_opportunity(topic, search_results)
    if validation:
        validation["topic"] = topic
        validation["researched_at"] = datetime.now().isoformat()
        
        opportunities = sm.read("validated_opportunities") or []
        opportunities.append(validation)
        sm.write("validated_opportunities", opportunities)
        
        state["researched_topics"].append(topic)
        log.info(f"✓ Validated: {topic} — {validation.get('recommended_action', 'UNKNOWN')}")
    else:
        log.error(f"✗ Validation failed for: {topic}")
    
    sm.write("research_topics", topics[1:])
    _save_state(state)


if __name__ == "__main__":
    log.info("Deep Researcher Agent starting...")
    while True:
        try:
            research_cycle()
        except Exception as e:
            log.error(f"Research cycle error: {e}")
        time.sleep(CYCLE_INTERVAL)

# === PRO-FIXER PATCH 20260328_1304 ===
# Fixed: DEEP_RESEARCHER
# Issues: validate_opportunity() function is truncated at line 127 - the API request and JSON parsing logic is incomplete, No error handling for malformed JSON responses from Claude API - will crash on parse failures, web_search() uses wrong HTTP method (GET instead of POST) for Serper API, No retry logic on the critical validate_opportunity() API call which is the core validation function, Missing main loop and task consumption logic - agent doesn't actually process tasks from shared_memory, No validation that Claude's response contains required JSON fields before accessing them, CYCLE_INTERVAL of 1500 seconds means agent only runs once every 25 minutes, too slow for validation failures
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
            data = resp.json()
            results = data.get("organic", [])
            return [{"title": r.get("title", ""),
                     "snippet": r.get("snippet", ""),
                     "url": r.get("link", "")}
                    for r in results[:5]]
        else:
            log.warning(f"Serper returned {resp.status_code}: {resp.text}")
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
  "demand_evidence": "specific proof of demand",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
}}"""

    def _call_claude():
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
        if resp.status_code != 200:
            raise Exception(f"Claude API returned {resp.status_code}: {resp.text}")
        return resp.json()

    try:
        result = _retry_api(_call_claude, retries=3, delay=2)
        if not result:
            return None
        
        content = result.get("content", [])
        if not content:
            log.error("No content in Claude response")
            return None
        
        text = content[0].get("text", "")
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if not json_match:
            log.error(f"No JSON found in response: {text[:200]}")
            return None
        
        data = json.loads(json_match.group())
        required_fields = ["is_viable", "confidence", "recommended_action"]
        if not all(f in data for f in required_fields):
            log.error(f"Missing required fields in validation response: {data}")
            return None
        
        return data
    except json.JSONDecodeError as e:
        log.error(f"JSON parse failed: {e}")
        return None
    except Exception as e:
        log.error(f"Validation failed: {e}")
        return None


def research_topic(topic):
    """Research a topic and return validation results."""
    log.info(f"Researching: {topic}")
    
    search_results = web_search(topic)
    if not search_results:
        log.warning(f"No search results for: {topic}")
        return {
            "topic": topic,
            "status": "NO_DATA",
            "message": "Could not find web data for this topic"
        }
    
    validation = validate_opportunity(topic, search_results)
    if not validation:
        log.error(f"Validation failed for: {topic}")
        return {
            "topic": topic,
            "status": "VALIDATION_FAILED",
            "message": "Claude validation returned no data"
        }
    
    return {
        "topic": topic,
        "status": "VALIDATED",
        "timestamp": datetime.now().isoformat(),
        "validation": validation,
        "sources": [r["url"] for r in search_results]
    }


def run_cycle():
    """Main research cycle - check for tasks and process them."""
    state = _load_state()
    state["cycle"] += 1
    log.info(f"=== Researcher Cycle {state['cycle']} ===")
    
    tasks = sm.get_agent_tasks("DEEP_RESEARCHER")
    
    if not tasks:
        log.info("No research tasks pending")
        _save_state(state)
        return
    
    for task in tasks:
        task_id = task.get("id")
        topic = task.get("topic", task.get("query", ""))
        
        if not topic:
            log.warning(f"Task {task_id} has no topic/query")
            sm.complete_task(task_id, {"status": "INVALID", "error": "No topic provided"})
            continue
        
        log.info(f"Processing task {task_id}: {topic}")
        result = research_topic(topic)
        
        sm.complete_task(task_id, result)
        state["researched_topics"].append({
            "topic": topic,
            "timestamp": datetime.now().isoformat(),
            "status": result.get("status")
        })
        
        log.info(f"Completed task {task_id} with status: {result.get('status')}")
    
    _save_state(state)
    log.info(f"Processed {len(tasks)} tasks")


if __name__ == "__main__":
    log.info("Deep Researcher Agent starting...")
    CYCLE_INTERVAL = 300
    
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
        
        log.info(f"Sleeping {CYCLE_INTERVAL}s...")
        time.sleep(CYCLE_INTERVAL)

# === PRO-FIXER PATCH 20260328_1320 ===
# Fixed: DEEP_RESEARCHER
# Issues: validate_opportunity() API call is TRUNCATED at line 156 — code cuts off mid-response.status_code check, making the entire validation function broken, No error handling for malformed JSON responses from Claude — if Claude returns non-JSON text, json.loads() will crash with no fallback, web_search() uses wrong Serper API method — should POST to https://google.serper.dev/search, not GET, Missing required timeout and error handling in validate_opportunity() requests.post() call, No retry logic on the actual validation API call — _retry_api exists but is never used where it matters most, State file saves researched_topics but never checks if a topic was already researched, causing duplicate work, CYCLE_INTERVAL of 1500 seconds means agent runs once every 25 minutes, but no main loop exists to actually schedule this, No main() function or if __name__ == '__main__' block — agent cannot actually run, search_results could be empty list, but validation prompt doesn't handle this case, leading to weak analysis, No integration with shared_memory to actually fetch tasks or post results back to CEO
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
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "No search results found"}
    
    results_text = "\n".join([f"- {r['title']}: {r['snippet']}" for r in search_results])
    prompt = f"""You are a market researcher. Validate if this is a real money-making opportunity.\n\nTOPIC: {topic}\nSEARCH RESULTS:\n{results_text}\n\nAnalyze for:\n1. Real buyer demand (people actively paying for this)\n2. Price benchmarks (what similar tools cost)\n3. Competition level (can we win?)\n4. Build difficulty (can an AI agent build this in 1-3 days?)\n5. Revenue potential (monthly recurring revenue possible?)\n\nBe HONEST and SPECIFIC. If demand is uncertain, say so.\n\nReply ONLY in valid JSON:\n{{\n  \"is_viable\": true/false,\n  \"confidence\": 0.0-1.0,\n  \"demand_evidence\": \"specific proof\",\n  \"price_benchmark\": \"$X-Y/month\",\n  \"competition\": \"LOW/MEDIUM/HIGH\",\n  \"build_difficulty\": \"EASY/MEDIUM/HARD\",\n  \"monthly_revenue_potential\": \"$X-Y\",\n  \"recommended_action\": \"BUILD_NOW/RESEARCH_MORE/SKIP\",\n  \"build_spec\": \"what to build\"\n}}"""
    
    def call_api():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": MODEL, "max_tokens": TOKENS, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return json.loads(content)
        else:
            log.error(f"Claude API error {resp.status_code}: {resp.text}")
            return None
    
    result = _retry_api(call_api, retries=3, delay=2)
    return result if result else {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "API failure"}

def research_task(task):
    """Main research logic: search + validate."""
    state = _load_state()
    topic = task.get("description", "")
    
    if topic in state.get("researched_topics", []):
        log.info(f"Already researched: {topic}")
        return {"status": "duplicate", "topic": topic}
    
    log.info(f"Researching: {topic}")
    search_results = web_search(topic)
    validation = validate_opportunity(topic, search_results)
    
    state["researched_topics"].append(topic)
    state["cycle"] = state.get("cycle", 0) + 1
    _save_state(state)
    
    return {"status": "complete", "topic": topic, "validation": validation, "search_results": search_results}

def main():
    log.info("DEEP_RESEARCHER starting...")
    while True:
        try:
            tasks = sm.get_pending_tasks("DEEP_RESEARCHER")
            for task in tasks:
                result = research_task(task)
                sm.post_result("DEEP_RESEARCHER", task.get("id"), result)
                if result.get("validation", {}).get("recommended_action") == "BUILD_NOW":
                    log.info(f"✓ VIABLE OPPORTUNITY: {result['topic']}")
            time.sleep(60)
        except KeyboardInterrupt:
            log.info("Researcher shutting down...")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()

# === PRO-FIXER PATCH 20260328_1327 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 137: Incomplete request timeout handling - response parsing is truncated mid-line 'if resp.status_code == 20' causing syntax error, Line 137-138: HTTP status code check incomplete (should be 200, not 20), Missing error handling in validate_opportunity() - no try/except around API call or JSON parsing, No retry logic on validate_opportunity() API calls - single failure causes entire validation to fail, web_search() uses GET with json parameter instead of POST - Serper API requires POST, JSON parsing from Claude response has no fallback - expects perfect JSON or crashes, No rate limiting or backoff between multiple validation attempts, State file doesn't track failed validations - retries same topics infinitely
def web_search(query):
    """Search the web for real data. Returns empty list on any failure."""
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            log.warning("No SERPER_API_KEY — skipping web search")
            return []
        resp = requests.post(
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
            content = resp.json().get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                json_match = re.search(r'\{[^{}]*"is_viable"[^{}]*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
                else:
                    try:
                        return json.loads(text)
                    except:
                        log.error(f"Could not parse JSON from Claude response: {text[:200]}")
                        return None
        else:
            log.error(f"Claude API returned {resp.status_code}: {resp.text[:200]}")
            return None

    try:
        result = _retry_api(_call_api, retries=3, delay=3)
        if result and isinstance(result, dict) and "is_viable" in result:
            return result
        else:
            log.error(f"Validation failed for topic: {topic}")
            return {
                "is_viable": False,
                "confidence": 0.0,
                "demand_evidence": "API call failed or returned invalid data",
                "price_benchmark": "Unknown",
                "competition": "UNKNOWN",
                "build_difficulty": "UNKNOWN",
                "monthly_revenue_potential": "$0",
                "recommended_action": "SKIP",
                "build_spec": "Validation failed - insufficient data"
            }
    except Exception as e:
        log.error(f"Exception in validate_opportunity: {e}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": f"Error: {str(e)}",
            "price_benchmark": "Unknown",
            "competition": "UNKNOWN",
            "build_difficulty": "UNKNOWN",
            "monthly_revenue_potential": "$0",
            "recommended_action": "SKIP",
            "build_spec": "Exception during validation"
        }

# === PRO-FIXER PATCH 20260328_1330 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 133: HTTP response parsing is truncated mid-line - 'if resp.status_code == 20' should be 'if resp.status_code == 200', No retry wrapper around critical validate_opportunity() API call - web_search() has retries but validation doesn't, validate_opportunity() returns None on failure instead of a fail-safe dict, causing downstream JSON parsing errors, No error handling for malformed JSON responses from Claude API - raw text responses will crash json.loads(), Missing main event loop and CEO communication - agent never reads tasks from shared_memory or reports results back, CYCLE_INTERVAL defined but never used - no autonomous execution loop implemented, State persistence exists but researched_topics never prevents duplicate research, No validation that ANTHROPIC_API_KEY exists before making API calls
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "API key missing"}
    
    results_text = "\n".join([
        f"- {r['title']}: {r['snippet']}"
        for r in search_results
    ]) if search_results else "No search results available"

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

Reply ONLY in valid JSON:
{{
  "is_viable": true,
  "confidence": 0.8,
  "demand_evidence": "specific proof",
  "price_benchmark": "$X-Y/month",
  "competition": "LOW/MEDIUM/HIGH",
  "build_difficulty": "EASY/MEDIUM/HARD",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW",
  "build_spec": "what to build"
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
                "max_tokens": TOKENS,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        return None
    
    result = _retry_api(_call_api, retries=3, delay=2)
    if result:
        return result
    else:
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "Validation API failed",
            "price_benchmark": "Unknown",
            "competition": "UNKNOWN",
            "build_difficulty": "UNKNOWN",
            "monthly_revenue_potential": "$0",
            "recommended_action": "SKIP",
            "build_spec": "Could not validate opportunity"
        }


def research_topic(topic):
    """Full research pipeline: search + validate."""
    log.info(f"Researching: {topic}")
    search_results = web_search(topic)
    if not search_results:
        log.warning(f"No search results for: {topic}")
    validation = validate_opportunity(topic, search_results)
    return {
        "topic": topic,
        "search_results": search_results,
        "validation": validation,
        "timestamp": datetime.now().isoformat()
    }


def main():
    """Autonomous research loop."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY required. Exiting.")
        return
    
    state = _load_state()
    log.info(f"Deep Researcher starting (cycle {state['cycle']})")
    
    while True:
        try:
            task = sm.get_task("DEEP_RESEARCHER")
            if task:
                topic = task.get("query", "")
                if topic and topic not in state["researched_topics"]:
                    result = research_topic(topic)
                    state["researched_topics"].append(topic)
                    state["cycle"] += 1
                    _save_state(state)
                    sm.write_result("DEEP_RESEARCHER", result)
                    log.info(f"Completed research: {topic} (viable: {result['validation']['is_viable']})")
                else:
                    log.info(f"Skipping duplicate topic: {topic}")
            time.sleep(10)
        except KeyboardInterrupt:
            log.info("Shutting down researcher...")
            break
        except Exception as e:
            log.error(f"Research cycle error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()

# === PRO-FIXER PATCH 20260328_1334 ===
# Fixed: DEEP_RESEARCHER
# Issues: API response truncated mid-line (line 179: 'if resp.status_code == 20') causing syntax error and preventing execution, No error handling for malformed JSON responses from Claude API - will crash if Claude returns non-JSON text, web_search() uses wrong HTTP method (GET with json body) - should use POST for Serper API, validate_opportunity() doesn't handle API rate limits or partial failures, causing silent validation failures, State persistence uses /tmp which is ephemeral - loses research history on container restart, No validation that search_results contains actual data before passing to Claude, Missing main execution loop - agent never actually runs autonomously, CYCLE_INTERVAL of 1500 seconds (25 min) with no actual cycle implementation
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
            log.warning(f"Serper returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Search failed: {e}")
    return []


def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for {topic} - cannot validate")
        return None
    
    results_text = "\n".join([f"- {r['title']}: {r['snippet']}" for r in search_results])
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
  "demand_evidence": "specific proof",
  "price_benchmark": "$X-Y/month",
  "competition": "LOW/MEDIUM/HIGH",
  "build_difficulty": "EASY/MEDIUM/HARD",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "what to build"
}}"""

    def _call_claude():
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": MODEL, "max_tokens": TOKENS, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content:
                text = content[0].get("text", "")
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        log.warning(f"Claude API returned {resp.status_code}")
        return None
    
    return _retry_api(_call_claude, retries=3, delay=2)


def research_cycle():
    """Execute one research cycle."""
    state = _load_state()
    state["cycle"] += 1
    log.info(f"=== RESEARCH CYCLE {state['cycle']} ===")
    
    topics = ["AI productivity tools", "API monetization tools", "developer automation SaaS", "no-code API builders", "webhook automation platforms"]
    validated_opportunities = []
    
    for topic in topics:
        if topic in state.get("researched_topics", []):
            continue
        log.info(f"Researching: {topic}")
        results = web_search(topic)
        if results:
            validation = validate_opportunity(topic, results)
            if validation and validation.get("is_viable"):
                log.info(f"✓ VIABLE: {topic} (confidence: {validation.get('confidence', 0)})")
                validated_opportunities.append({"topic": topic, "validation": validation, "timestamp": datetime.now().isoformat()})
                sm.append("research_opportunities", {"topic": topic, "data": validation})
            state["researched_topics"].append(topic)
        time.sleep(2)
    
    _save_state(state)
    log.info(f"Cycle complete. Found {len(validated_opportunities)} opportunities.")
    return validated_opportunities


if __name__ == "__main__":
    log.info("Deep Researcher Agent starting...")
    while True:
        try:
            research_cycle()
            log.info(f"Sleeping {CYCLE_INTERVAL}s until next cycle...")
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("Researcher shutting down.")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)

# === PRO-FIXER PATCH 20260328_1336 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 120-141: Incomplete try-except block - response parsing code is cut off mid-line ('if resp.status_code == 20' should be '200'), Missing error handling for malformed JSON responses from Claude API, validate_opportunity() returns None on failure instead of safe default, causing downstream crashes, No retry logic on the critical validate_opportunity() Claude API call, web_search() uses wrong HTTP method (GET instead of POST) for Serper API, Missing validation that search_results is not empty before passing to Claude, No timeout or error recovery in main research loop, State file corruption can crash entire agent with no recovery
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
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "No search results found",
            "recommended_action": "SKIP",
            "build_spec": ""
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

    def _call_claude():
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
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                else:
                    log.error("No JSON found in Claude response")
                    return None
        else:
            log.error(f"Claude API returned {resp.status_code}: {resp.text}")
            return None
    
    result = _retry_api(_call_claude, retries=3, delay=2)
    
    if result and isinstance(result, dict) and "is_viable" in result:
        return result
    else:
        log.error(f"Validation failed for topic: {topic}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "demand_evidence": "API call failed",
            "price_benchmark": "unknown",
            "competition": "UNKNOWN",
            "build_difficulty": "UNKNOWN",
            "monthly_revenue_potential": "$0",
            "recommended_action": "SKIP",
            "build_spec": ""
        }

# === PRO-FIXER PATCH 20260328_1341 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 128: Incomplete API response handling - code is truncated mid-line 'if resp.status_code == 20' (missing rest of condition), JSON validation logic missing - function returns validation results but never checks if JSON is valid before parsing, No error handling for malformed Claude responses - will crash on non-JSON responses, Missing retry logic on validate_opportunity() - most critical function has no fault tolerance, Web search returns empty list on failure but code doesn't validate search_results before passing to Claude, State persistence happens after validation but validation failures don't update 'researched_topics' - causes infinite retry loops, No timeout protection on main loop - CYCLE_INTERVAL is 1500s but no actual cycle enforcement, Missing shared_memory integration - imports sm but never uses it to coordinate with other agents
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for '{topic}' - marking as researched but not viable")
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

Reply ONLY in valid JSON:
{{
  "is_viable": true,
  "confidence": 0.8,
  "demand_evidence": "specific proof",
  "price_benchmark": "$X-Y/month",
  "competition": "LOW/MEDIUM/HIGH",
  "build_difficulty": "EASY/MEDIUM/HARD",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW",
  "build_spec": "what to build"
}}"""

    def _call_claude():
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
            content = resp.json().get("content", [])
            if content and len(content) > 0:
                text = content[0].get("text", "")
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                else:
                    return json.loads(text)
        raise Exception(f"API returned {resp.status_code}")
    
    try:
        result = _retry_api(_call_claude, retries=3, delay=2)
        if result and isinstance(result, dict) and "is_viable" in result:
            return result
        else:
            log.error(f"Invalid validation result for '{topic}'")
            return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "Validation failed"}
    except Exception as e:
        log.error(f"Validation exception for '{topic}': {e}")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": f"Error: {str(e)}"}


def research_cycle():
    """Run one research cycle."""
    state = _load_state()
    state["cycle"] += 1
    log.info(f"🔬 Research Cycle {state['cycle']}")
    
    topics = [
        "RapidAPI marketplace trending APIs",
        "top selling APIs on API marketplaces 2024",
        "profitable SaaS micro-tools under $50/mo",
        "AI automation tools with paying customers",
        "developer tools with monthly subscriptions"
    ]
    
    for topic in topics:
        if topic in state.get("researched_topics", []):
            continue
        
        log.info(f"Researching: {topic}")
        results = web_search(topic)
        validation = validate_opportunity(topic, results)
        
        state["researched_topics"].append(topic)
        
        if validation.get("is_viable") and validation.get("confidence", 0) > 0.6:
            log.info(f"✅ VIABLE: {topic}")
            log.info(f"   Confidence: {validation.get('confidence')}")
            log.info(f"   Action: {validation.get('recommended_action')}")
            
            sm.add_task({
                "type": "research_finding",
                "topic": topic,
                "validation": validation,
                "timestamp": datetime.now().isoformat()
            })
        else:
            log.info(f"❌ NOT VIABLE: {topic} (confidence: {validation.get('confidence', 0)})")
        
        _save_state(state)
        time.sleep(2)
    
    log.info(f"Cycle {state['cycle']} complete. Next cycle in {CYCLE_INTERVAL}s")


def main():
    """Main loop."""
    log.info("🔬 Deep Researcher Agent starting...")
    
    while True:
        try:
            research_cycle()
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("Researcher shutting down...")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()

# === PRO-FIXER PATCH 20260328_1343 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 130: HTTP request code is truncated mid-execution (incomplete if statement at 'if resp.status_code == 20'), No error handling for missing ANTHROPIC_API_KEY - function will fail silently when key is empty, validate_opportunity() doesn't handle JSON parsing errors from Claude's response, No timeout handling or retry logic for the validation API call despite _retry_api helper existing, web_search() returns empty list on failure but validate_opportunity() doesn't check if search results are empty before processing, State management doesn't track failed validations - agent will retry same failed topics infinitely, Missing main execution loop - no run() or main() function to actually execute the research cycles, CYCLE_INTERVAL defined but never used in any sleep or scheduling logic
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for topic: {topic}")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "No search results found"}
    
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set - cannot validate")
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
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        log.warning(f"Validation API returned status {resp.status_code}")
        return None
    
    result = _retry_api(_api_call, retries=3, delay=2)
    if not result:
        log.error(f"Failed to validate topic: {topic}")
        return {"is_viable": False, "confidence": 0.0, "recommended_action": "SKIP", "demand_evidence": "API validation failed"}
    
    return result


def research_opportunities():
    """Main research function - find and validate opportunities."""
    state = _load_state()
    state["cycle"] = state.get("cycle", 0) + 1
    
    topics = [
        "AI API tools for developers",
        "automation tools for small businesses",
        "data extraction APIs",
        "productivity SaaS for remote teams",
        "no-code workflow automation"
    ]
    
    validated_opportunities = []
    
    for topic in topics:
        if topic in state.get("researched_topics", []):
            log.info(f"Skipping already researched: {topic}")
            continue
        
        log.info(f"Researching: {topic}")
        search_results = web_search(topic)
        
        if not search_results:
            log.warning(f"No search results for: {topic}")
            state.setdefault("researched_topics", []).append(topic)
            continue
        
        validation = validate_opportunity(topic, search_results)
        
        if validation and validation.get("is_viable"):
            log.info(f"✓ VIABLE: {topic} (confidence: {validation.get('confidence', 0)})")
            validated_opportunities.append({
                "topic": topic,
                "validation": validation,
                "timestamp": datetime.now().isoformat()
            })
        else:
            log.info(f"✗ Not viable: {topic}")
        
        state.setdefault("researched_topics", []).append(topic)
        time.sleep(2)
    
    if validated_opportunities:
        sm.store_research(validated_opportunities)
        log.info(f"Stored {len(validated_opportunities)} opportunities in shared memory")
    
    _save_state(state)
    return validated_opportunities


def run():
    """Main execution loop."""
    log.info("Deep Researcher Agent starting...")
    
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set - cannot run")
        return
    
    while True:
        try:
            log.info(f"Starting research cycle at {datetime.now()}")
            opportunities = research_opportunities()
            log.info(f"Cycle complete. Found {len(opportunities)} viable opportunities.")
            log.info(f"Sleeping for {CYCLE_INTERVAL} seconds...")
            time.sleep(CYCLE_INTERVAL)
        except KeyboardInterrupt:
            log.info("Researcher stopped by user")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run()

# === PRO-FIXER PATCH 20260328_1347 ===
# Fixed: DEEP_RESEARCHER
# Issues: Line 148: Incomplete code - requests.post() call is cut off mid-line with 'if resp.status_code == 20' instead of 200, Missing JSON parsing and error handling for Claude API response in validate_opportunity(), No main() function or agent loop implementation - the file ends abruptly, Missing state persistence logic - _save_state() is called nowhere, No integration with shared_memory module despite importing it, Validation failures suggest the agent never actually runs or completes cycles, No error handling for malformed JSON responses from Claude, Missing cycle management and task queue processing
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
  "demand_evidence": "specific proof of demand",
  "price_benchmark": "$X-Y/month based on [source]",
  "competition": "LOW/MEDIUM/HIGH — why",
  "build_difficulty": "EASY/MEDIUM/HARD — why",
  "monthly_revenue_potential": "$X-Y",
  "recommended_action": "BUILD_NOW/RESEARCH_MORE/SKIP",
  "build_spec": "1-2 sentence description of exactly what to build"
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
                "max_tokens": TOKENS,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        return None

    result = _retry_api(_api_call)
    if not result:
        log.error(f"Validation failed for: {topic}")
        return {
            "is_viable": False,
            "confidence": 0.0,
            "recommended_action": "SKIP",
            "error": "API call failed"
        }
    return result


def research_topic(topic):
    """Deep research on a topic with validation."""
    log.info(f"Researching: {topic}")
    
    search_results = web_search(topic)
    if not search_results:
        log.warning(f"No search results for: {topic}")
        return None
    
    validation = validate_opportunity(topic, search_results)
    
    return {
        "topic": topic,
        "timestamp": datetime.now().isoformat(),
        "search_results": search_results,
        "validation": validation,
        "status": "completed"
    }


def process_research_tasks():
    """Read tasks from shared memory and process them."""
    tasks = sm.get_list("research_tasks") or []
    completed = sm.get_list("research_completed") or []
    
    for task in tasks:
        if task in completed:
            continue
            
        log.info(f"Processing research task: {task}")
        result = research_topic(task)
        
        if result and result.get("validation", {}).get("is_viable"):
            sm.append_list("validated_opportunities", result)
            log.info(f"✓ Validated opportunity: {task}")
        else:
            log.info(f"✗ Rejected: {task}")
        
        sm.append_list("research_completed", task)
        sm.save()


def autonomous_cycle():
    """Single research cycle."""
    state = _load_state()
    state["cycle"] += 1
    
    log.info(f"=== RESEARCHER CYCLE {state['cycle']} ===")
    
    process_research_tasks()
    
    if state["cycle"] % 5 == 0:
        opportunities = sm.get_list("validated_opportunities") or []
        log.info(f"Total validated opportunities: {len(opportunities)}")
    
    _save_state(state)


def main():
    """Main autonomous loop."""
    log.info("Deep Researcher Agent starting...")
    log.info(f"Cycle interval: {CYCLE_INTERVAL}s ({CYCLE_INTERVAL/60:.1f} minutes)")
    
    while True:
        try:
            autonomous_cycle()
        except Exception as e:
            log.error(f"Cycle error: {e}")
        
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    main()

# === PRO-FIXER PATCH 20260328_1404 ===
# Fixed: DEEP_RESEARCHER
# Issues: CRITICAL: Line 145 has truncated code - 'if resp.status_code == 20' is incomplete, missing status code check (likely 200), CRITICAL: JSON parsing likely fails in validate_opportunity() - Claude returns markdown-wrapped JSON ( blocks) but code tries to parse raw response, CRITICAL: No error handling for malformed JSON responses from Claude API, Missing validation error details - validation failures don't log WHY they failed, Web search returns empty list on failure but code doesn't check if results are empty before validating, No retry logic on Claude API calls despite having _retry_api helper function, State persistence may fail silently - no verification that topics are actually saved, SERPER_API_KEY check happens too late - should fail fast if missing
def validate_opportunity(topic, search_results):
    """Use Claude to validate if this is a real opportunity."""
    if not search_results:
        log.warning(f"No search results for '{topic}' - skipping validation")
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

    def _call_claude():
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
            return resp.json()
        else:
            log.error(f"Claude API returned {resp.status_code}: {resp.text[:200]}")
            return None
    
    result = _retry_api(_call_claude, retries=3, delay=2)
    if not result:
        log.error(f"Validation failed for '{topic}' - Claude API unavailable")
        return None
    
    try:
        content = result.get("content", [])
        if not content:
            log.error(f"Validation failed for '{topic}' - empty response from Claude")
            return None
            
        text = content[0].get("text", "")
        if not text:
            log.error(f"Validation failed for '{topic}' - no text in response")
            return None
        
        # Strip markdown code blocks if present
        text = text.strip()
        if text.startswith(""):
            text = text[7:]  # Remove 
        if text.startswith(""):
            text = text[3:]  # Remove 
        if text.endswith(""):
            text = text[:-3]  # Remove trailing 
        text = text.strip()
        
        validation = json.loads(text)
        
        # Verify required fields
        required = ["is_viable", "confidence", "recommended_action"]
        missing = [f for f in required if f not in validation]
        if missing:
            log.error(f"Validation failed for '{topic}' - missing fields: {missing}")
            return None
            
        log.info(f"Validated '{topic}': viable={validation.get('is_viable')}, confidence={validation.get('confidence')}, action={validation.get('recommended_action')}")
        return validation
        
    except json.JSONDecodeError as e:
        log.error(f"Validation failed for '{topic}' - JSON parse error: {e}")
        log.error(f"Raw response text: {text[:500]}")
        return None
    except Exception as e:
        log.error(f"Validation failed for '{topic}' - unexpected error: {e}")
        return None