import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from app.db import get_pool
from app.governance import revoke, activate
from app.audit import verify_chain

logger = logging.getLogger(__name__)

router = APIRouter()

ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = ROOT / "app" / "templates"
SCORECARD_SCRIPT = ROOT / "scripts" / "adversarial_scorecard.py"

@router.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the main dashboard HTML."""
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard template not found.")
    return FileResponse(index_file)

@router.get("/api/status")
async def api_status():
    """
    Returns top-level stats and chain integrity status for all agents.
    """
    pool = get_pool()
    agents = await pool.fetch("SELECT id, status FROM agents")
    total_agents = len(agents)
    total_revoked = sum(1 for a in agents if a["status"] == "revoked")
    
    chain_integrity = True
    broken_at_row = None
    
    # Verify chain for all agents
    for agent in agents:
        valid, bad_id = await verify_chain(pool, agent["id"])
        if not valid:
            chain_integrity = False
            broken_at_row = bad_id
            break # Stop at first broken chain for the top level status
            
    return {
        "total_agents": total_agents,
        "total_revoked": total_revoked,
        "chain_integrity": chain_integrity,
        "broken_at_row": broken_at_row,
        "timestamp": datetime.now(tz=timezone.utc).isoformat()
    }

@router.get("/api/agents")
async def api_agents():
    """
    Returns a list of all agents and their ledger status.
    """
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, name, cap, spent, reserved, status FROM agents ORDER BY name"
    )
    
    agents = []
    for row in rows:
        agents.append({
            "id": row["id"],
            "name": row["name"],
            "cap": str(row["cap"]),
            "spent": str(row["spent"]),
            "reserved": str(row["reserved"]),
            "status": row["status"]
        })
        
    return agents

@router.get("/api/audit")
async def api_audit():
    """
    Returns the most recent audit log entries (limit 100).
    """
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, agent_id, tool_name, decision, reason, est_cost, actual_cost, ts, prev_hash, row_hash
        FROM audit_log
        ORDER BY id DESC
        LIMIT 100
        """
    )
    
    audit_entries = []
    for row in rows:
        audit_entries.append({
            "id": row["id"],
            "agent_id": row["agent_id"],
            "tool_name": row["tool_name"],
            "decision": row["decision"],
            "reason": row["reason"],
            "est_cost": str(row["est_cost"]) if row["est_cost"] is not None else None,
            "actual_cost": str(row["actual_cost"]) if row["actual_cost"] is not None else None,
            "ts": row["ts"].isoformat(),
            "prev_hash": row["prev_hash"],
            "row_hash": row["row_hash"]
        })
        
    broken_links = []
    agent_ids = set(r["agent_id"] for r in rows)
    for aid in agent_ids:
        valid, bad_id = await verify_chain(pool, aid)
        if not valid and bad_id:
            broken_links.append(bad_id)
            
    return {
        "entries": audit_entries,
        "broken_links": broken_links
    }

_cached_scorecard = None

@router.get("/api/scorecard")
async def api_scorecard():
    """
    Runs the adversarial scorecard script if not cached, or parses its output.
    """
    global _cached_scorecard
    if _cached_scorecard is not None:
        return _cached_scorecard
        
    python_exe = sys.executable
    try:
        process = await asyncio.create_subprocess_exec(
            python_exe, str(SCORECARD_SCRIPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        
        import re
        results = []
        for line in output.split("\n"):
            line = line.strip()
            # Match lines like: [1. Concurrent cap-breach] category → BLOCKED
            # or with mangled characters from Windows console
            match = re.match(r"^\[(.*?)\] category.*?(BLOCKED|FAILED)(.*)$", line)
            if match:
                name_part = match.group(1).strip()
                status = match.group(2)
                rest = match.group(3).strip()
                
                msg = ""
                if "(" in rest and ")" in rest:
                    msg = rest[rest.find("(")+1 : rest.rfind(")")]
                    
                results.append({
                    "name": name_part,
                    "status": status,
                    "message": msg
                })
                
        if results:
            _cached_scorecard = results
        return results
    except Exception as e:
        logger.error(f"Error running scorecard: {e}")
        return []

@router.post("/api/agents/{agent_id}/revoke")
async def api_revoke_agent(agent_id: str):
    pool = get_pool()
    success = await revoke(pool, agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok"}

@router.post("/api/agents/{agent_id}/activate")
async def api_activate_agent(agent_id: str):
    pool = get_pool()
    success = await activate(pool, agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok"}
