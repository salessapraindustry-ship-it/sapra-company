#!/usr/bin/env python3
# ================================================================
#  monitor_api.py — Exposes Google Sheet data as a REST API
#  Deploy on Railway — gives Claude a URL to read company status
#  GET /status    — full company status
#  GET /tasks     — all tasks
#  GET /agents    — all agent statuses
#  GET /revenue   — revenue data
# ================================================================

import os
import json
import logging
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Sapra Company Monitor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_SHEET_ID  = os.environ.get("GOOGLE_SHEET_ID", "")
MONITOR_API_KEY  = os.environ.get("MONITOR_API_KEY", "sapra2026")  # simple auth


def _get_sheet():
    """Get Google Sheets connection."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
        if creds_json:
            creds = Credentials.from_service_account_info(
                json.loads(creds_json),
                scopes=["https://spreadsheets.google.com/feeds",
                        "https://www.googleapis.com/auth/drive"]
            )
            client = gspread.authorize(creds)
            return client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        logging.error(f"Sheet connection failed: {e}")
    return None


def _read_tab(tab_name):
    """Read a tab from Google Sheets."""
    try:
        sheet = _get_sheet()
        if not sheet:
            return []
        ws = sheet.worksheet(tab_name)
        return ws.get_all_records()
    except Exception as e:
        logging.warning(f"Tab {tab_name} not found: {e}")
        return []


@app.get("/")
def root():
    return {
        "status": "Sapra Company Monitor API",
        "time": datetime.now().isoformat(),
        "endpoints": ["/status", "/tasks", "/agents", "/revenue", "/research", "/errors"]
    }


@app.get("/status")
def get_status(api_key: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    tasks   = _read_tab("Tasks")
    agents  = _read_tab("Agent Status")
    revenue = _read_tab("Revenue")

    done    = [t for t in tasks if t.get("status") == "DONE"]
    failed  = [t for t in tasks if t.get("status") == "FAILED"]
    active  = [t for t in tasks if t.get("status") not in ("DONE","FAILED","PENDING")]
    pending = [t for t in tasks if t.get("status") == "PENDING"]

    total_revenue = sum(float(r.get("amount", 0)) for r in revenue)

    active_agents  = [a for a in agents if a.get("status") == "ACTIVE"]
    degraded_agents= [a for a in agents if int(a.get("score", 10)) < 3]

    return {
        "timestamp":        datetime.now().isoformat(),
        "company_health":   "CRITICAL" if degraded_agents else "GOOD",
        "total_revenue":    f"${total_revenue:.2f}",
        "tasks": {
            "total":   len(tasks),
            "done":    len(done),
            "active":  len(active),
            "pending": len(pending),
            "failed":  len(failed),
        },
        "agents": {
            "total":    len(agents),
            "active":   len(active_agents),
            "degraded": [a.get("agent") for a in degraded_agents],
        },
        "latest_task": tasks[-1] if tasks else None,
        "latest_revenue": revenue[-1] if revenue else None,
    }


@app.get("/tasks")
def get_tasks(api_key: str = "", status: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    tasks = _read_tab("Tasks")
    if status:
        tasks = [t for t in tasks if t.get("status","").upper() == status.upper()]
    return {"count": len(tasks), "tasks": tasks}


@app.get("/agents")
def get_agents(api_key: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    agents = _read_tab("Agent Status")
    return {"count": len(agents), "agents": agents}


@app.get("/revenue")
def get_revenue(api_key: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    revenue = _read_tab("Revenue")
    total   = sum(float(r.get("amount", 0)) for r in revenue)
    return {
        "total_revenue": f"${total:.2f}",
        "entries":       len(revenue),
        "breakdown":     revenue
    }


@app.get("/research")
def get_research(api_key: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    research = _read_tab("Research")
    return {"count": len(research), "research": research}


@app.get("/errors")
def get_errors(api_key: str = ""):
    if api_key != MONITOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    logs = _read_tab("Agent Log")
    errors = [
        r for r in logs
        if "error" in str(r).lower()
        or "❌" in str(r)
        or "failed" in str(r).lower()
    ]
    return {"count": len(errors), "errors": errors[-20:]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
