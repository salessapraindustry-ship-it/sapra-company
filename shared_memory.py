#!/usr/bin/env python3
# ================================================================
#  shared_memory.py — Communication bus for all agents
#  Google Sheets is the shared memory — all agents read/write here
#  Tabs: Tasks | Agent Status | Reports | Research | Revenue
# ================================================================

import json
import os
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)

SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")
TIMEOUT     = 10  # seconds for all sheet operations

def _retry_sheet(fn, retries=3, delay=2):
    """Retry Google Sheets operations on failure."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                log.warning(f"Sheet retry {attempt+1}/{retries}: {e}")
                time.sleep(delay)
            else:
                log.error(f"Sheet operation failed after {retries} attempts: {e}")
                return None

# Task stages
STAGE_PENDING    = "PENDING"
STAGE_ASSIGNED   = "ASSIGNED"
STAGE_RESEARCH   = "RESEARCH"
STAGE_BUILD      = "BUILD"
STAGE_TEST       = "TEST"
STAGE_DEPLOY     = "DEPLOY"
STAGE_SELL       = "SELL"
STAGE_DONE       = "DONE"
STAGE_FAILED     = "FAILED"

# Agent names
AGENT_CEO        = "CEO"
AGENT_RESEARCHER = "DEEP_RESEARCHER"
AGENT_BACKEND    = "BACKEND_BUILDER"
AGENT_FRONTEND   = "FRONTEND_BUILDER"
AGENT_B2B        = "B2B_SELLER"
AGENT_FREELANCE  = "FREELANCE_SELLER"
AGENT_FIXER      = "PRO_FIXER"


def _get_sheet():
    """Get Google Sheets connection."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
        if not creds_json:
            if os.path.exists("service_account.json"):
                creds = Credentials.from_service_account_file(
                    "service_account.json",
                    scopes=["https://spreadsheets.google.com/feeds",
                            "https://www.googleapis.com/auth/drive"]
                )
            else:
                return None
        else:
            creds = Credentials.from_service_account_info(
                json.loads(creds_json),
                scopes=["https://spreadsheets.google.com/feeds",
                        "https://www.googleapis.com/auth/drive"]
            )
        client = gspread.authorize(creds)
        return client.open_by_key(SHEET_ID)
    except Exception as e:
        log.warning(f"Sheet connection failed: {e}")
        return None


def _get_or_create_tab(sheet, name, rows=500, cols=20):
    """Get or create a worksheet tab."""
    try:
        return sheet.worksheet(name)
    except Exception:
        try:
            return sheet.add_worksheet(name, rows=rows, cols=cols)
        except Exception as e:
            log.error(f"Cannot create tab {name}: {e}")
            return None


# ================================================================
#  TASK QUEUE — CEO writes, agents read and update
# ================================================================

def post_task(task_id, title, description, assigned_to,
              priority="NORMAL", context=None):
    """CEO posts a task for an agent."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return False
        ws = _get_or_create_tab(sheet, "Tasks")
        if not ws:
            return False
        # Init headers if empty
        if ws.row_count == 0 or not ws.row_values(1):
            ws.append_row([
                "task_id","title","description","assigned_to",
                "status","priority","created","updated",
                "result","context"
            ])
        ws.append_row([
            task_id, title, description[:200], assigned_to,
            STAGE_PENDING, priority,
            datetime.now().isoformat(), datetime.now().isoformat(),
            "", json.dumps(context or {})
        ])
        log.info(f"📋 Task posted: [{task_id}] {title} → {assigned_to}")
        return True
    except Exception as e:
        log.error(f"post_task error: {e}")
        return False


def get_my_tasks(agent_name):
    """Agent reads its assigned tasks."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = _get_or_create_tab(sheet, "Tasks")
        if not ws:
            return []
        records = ws.get_all_records()
        return [
            r for r in records
            if r.get("assigned_to") == agent_name
            and r.get("status") not in (STAGE_DONE, STAGE_FAILED)
        ]
    except Exception as e:
        log.error(f"get_my_tasks error: {e}")
        return []


def update_task(task_id, status, result=""):
    """Agent updates task status."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return False
        ws = _get_or_create_tab(sheet, "Tasks")
        if not ws:
            return False
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if str(row.get("task_id")) == str(task_id):
                ws.update_cell(i, 5, status)  # status col
                ws.update_cell(i, 8, datetime.now().isoformat())  # updated
                if result:
                    ws.update_cell(i, 9, result[:500])  # result
                log.info(f"✅ Task {task_id} → {status}")
                return True
        return False
    except Exception as e:
        log.error(f"update_task error: {e}")
        return False


def get_all_tasks():
    """CEO reads all tasks."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = _get_or_create_tab(sheet, "Tasks")
        if not ws:
            return []
        return ws.get_all_records()
    except Exception as e:
        log.error(f"get_all_tasks error: {e}")
        return []


# ================================================================
#  AGENT STATUS — Each agent reports health every cycle
# ================================================================

def report_status(agent_name, status, current_task="",
                  cycles_done=0, last_output="", score=0):
    """Agent reports its health/status."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws = _get_or_create_tab(sheet, "Agent Status")
        if not ws:
            return
        if not ws.row_values(1):
            ws.append_row([
                "agent","status","current_task","cycles_done",
                "last_output","score","last_seen"
            ])
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if row.get("agent") == agent_name:
                ws.update(f"A{i}:G{i}", [[
                    agent_name, status, current_task, cycles_done,
                    last_output[:200], score, datetime.now().isoformat()
                ]])
                return
        ws.append_row([
            agent_name, status, current_task, cycles_done,
            last_output[:200], score, datetime.now().isoformat()
        ])
    except Exception as e:
        log.warning(f"report_status error: {e}")


def get_all_agent_statuses():
    """CEO/Fixer reads all agent statuses."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = _get_or_create_tab(sheet, "Agent Status")
        if not ws:
            return []
        return ws.get_all_records()
    except Exception as e:
        log.error(f"get_all_agent_statuses error: {e}")
        return []


# ================================================================
#  RESEARCH BOARD — Deep Researcher posts findings
# ================================================================

def post_research(topic, summary, opportunities, data_sources,
                  confidence=0.0):
    """Deep Researcher posts validated findings."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws = _get_or_create_tab(sheet, "Research")
        if not ws:
            return
        if not ws.row_values(1):
            ws.append_row([
                "topic","summary","opportunities",
                "data_sources","confidence","posted_at"
            ])
        ws.append_row([
            topic, summary[:300],
            json.dumps(opportunities)[:500],
            json.dumps(data_sources)[:300],
            confidence, datetime.now().isoformat()
        ])
        log.info(f"🔬 Research posted: {topic}")
    except Exception as e:
        log.error(f"post_research error: {e}")


def get_latest_research(limit=5):
    """Builders/Sellers read latest research."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = _get_or_create_tab(sheet, "Research")
        if not ws:
            return []
        records = ws.get_all_records()
        return records[-limit:] if records else []
    except Exception as e:
        log.error(f"get_latest_research error: {e}")
        return []


# ================================================================
#  REVENUE TRACKER — Sellers post every dollar earned
# ================================================================

def log_revenue(source, amount, description, agent_name):
    """Seller logs a revenue event."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws = _get_or_create_tab(sheet, "Revenue")
        if not ws:
            return
        if not ws.row_values(1):
            ws.append_row([
                "source","amount","description","agent","timestamp"
            ])
        ws.append_row([
            source, amount, description[:200],
            agent_name, datetime.now().isoformat()
        ])
        log.info(f"💰 Revenue logged: ${amount} from {source}")
    except Exception as e:
        log.error(f"log_revenue error: {e}")


def get_total_revenue():
    """CEO reads total revenue."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return 0.0
        ws = _get_or_create_tab(sheet, "Revenue")
        if not ws:
            return 0.0
        records = ws.get_all_records()
        return sum(float(r.get("amount", 0)) for r in records)
    except Exception as e:
        log.error(f"get_total_revenue error: {e}")
        return 0.0


# ================================================================
#  FIXER LOG — Pro-Fixer posts improvement reports
# ================================================================

def post_fixer_report(agent_improved, metric_before, metric_after,
                      changes_made, cycle_number):
    """Pro-Fixer posts improvement report every 3 days."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws = _get_or_create_tab(sheet, "Fixer Reports")
        if not ws:
            return
        if not ws.row_values(1):
            ws.append_row([
                "agent","metric_before","metric_after",
                "improvement_pct","changes","cycle","timestamp"
            ])
        improvement = 0
        try:
            improvement = round(
                (float(metric_after) - float(metric_before))
                / max(float(metric_before), 0.001) * 100, 1
            )
        except Exception:
            pass
        ws.append_row([
            agent_improved, metric_before, metric_after,
            f"{improvement}%", changes_made[:300],
            cycle_number, datetime.now().isoformat()
        ])
        log.info(f"🔧 Fixer report: {agent_improved} improved {improvement}%")
    except Exception as e:
        log.error(f"post_fixer_report error: {e}")


def get_agent_error_logs(limit=50):
    """Pro-Fixer reads error logs from Agent Log tab."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = _get_or_create_tab(sheet, "Agent Log")
        if not ws:
            return []
        records = ws.get_all_records()
        errors = [r for r in records if "error" in str(r).lower()
                  or "❌" in str(r) or "failed" in str(r).lower()]
        return errors[-limit:]
    except Exception as e:
        log.error(f"get_agent_error_logs error: {e}")
        return []
