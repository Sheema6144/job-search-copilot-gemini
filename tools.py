"""
Tool implementations for the Job Search Copilot agent.

Every tool here is a plain Python function with a JSON-serializable
return value, plus a matching "spec" dict describing it to the LLM in
JSON-schema-based tool/function-calling format. Keeping tools as pure
functions (no dependency on the agent loop) makes them easy to unit
test in isolation -- see tests/test_tools.py.

Data sources used (all free, no API key required -- good for a demo /
portfolio project; see README for how you'd swap in a paid provider
like Adzuna, Indeed Publisher, or LinkedIn Jobs API for production use):
  - Remotive   (https://remotive.com/api/remote-jobs)   -- remote jobs, supports search
  - Arbeitnow  (https://www.arbeitnow.com/api/job-board-api) -- general job board, client-side filtered
  - DuckDuckGo HTML search -- fallback for company research / general queries

Application tracking is a local JSON file (applications.json) so the
whole thing runs with zero external database setup.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
APPLICATIONS_FILE = os.path.join(_DIR, "applications.json")

_TAG_STRIP = re.compile(r"<[^>]+>")


def _clean_html(text: str, max_len: int = 600) -> str:
    text = _TAG_STRIP.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


# ---------------------------------------------------------------------------
# 1. Job search (Remotive + Arbeitnow, both free / no API key)
# ---------------------------------------------------------------------------

def _search_remotive(query: str, max_results: int) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": query, "limit": max_results},
            timeout=10,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
    except (requests.RequestException, ValueError):
        return []

    return [
        {
            "source": "remotive",
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("candidate_required_location"),
            "remote": True,
            "job_type": j.get("job_type"),
            "salary": j.get("salary") or None,
            "url": j.get("url"),
            "description": _clean_html(j.get("description", "")),
            "published": j.get("publication_date"),
        }
        for j in jobs[:max_results]
    ]


def _search_arbeitnow(query: str, max_results: int) -> list[dict[str, Any]]:
    try:
        resp = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=10)
        resp.raise_for_status()
        jobs = resp.json().get("data", [])
    except (requests.RequestException, ValueError):
        return []

    query_lower = query.lower()
    matched = [
        j
        for j in jobs
        if query_lower in (j.get("title", "") + " " + " ".join(j.get("tags", []))).lower()
    ]

    return [
        {
            "source": "arbeitnow",
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "remote": j.get("remote", False),
            "job_type": ", ".join(j.get("job_types", [])) or None,
            "salary": None,
            "url": j.get("url"),
            "description": _clean_html(j.get("description", "")),
            "published": j.get("created_at"),
        }
        for j in matched[:max_results]
    ]


def search_jobs(query: str, remote_only: bool = False, max_results: int = 8) -> dict[str, Any]:
    """Search live job postings for a role/keyword across free job boards."""
    per_source = max(3, max_results // 2 + 1)
    results = _search_remotive(query, per_source) + _search_arbeitnow(query, per_source)

    if remote_only:
        results = [r for r in results if r.get("remote")]

    # de-dupe by (title, company)
    seen = set()
    deduped = []
    for r in results:
        key = (r.get("title", "").lower(), (r.get("company") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return {"query": query, "count": len(deduped[:max_results]), "jobs": deduped[:max_results]}


# ---------------------------------------------------------------------------
# 2. General web search (DuckDuckGo, no API key) -- for company research etc.
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the general web -- useful for researching a company, role, or salary range."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (job-search-copilot)"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return {"error": f"search request failed: {exc}"}

    html = resp.text
    link_pattern = re.compile(
        r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', re.S
    )
    snippet_pattern = re.compile(r'<a class="result__snippet"[^>]*>(.*?)</a>', re.S)

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    results = []
    for i, (url, title) in enumerate(links[:max_results]):
        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        results.append({"title": _clean_html(title), "url": url, "snippet": snippet})

    return {"query": query, "results": results}


# ---------------------------------------------------------------------------
# 3. Application tracker (local JSON persistence)
# ---------------------------------------------------------------------------

def _load_applications() -> list[dict[str, Any]]:
    if not os.path.exists(APPLICATIONS_FILE):
        return []
    try:
        with open(APPLICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _write_applications(apps: list[dict[str, Any]]) -> None:
    with open(APPLICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(apps, f, indent=2)


def track_application(
    company: str,
    role: str,
    url: str | None = None,
    status: str = "saved",
    notes: str | None = None,
) -> dict[str, Any]:
    """Add or update a tracked job application, keyed by (company, role)."""
    apps = _load_applications()
    now = datetime.now(timezone.utc).isoformat()

    for entry in apps:
        if entry["company"].lower() == company.lower() and entry["role"].lower() == role.lower():
            entry["status"] = status
            entry["url"] = url or entry.get("url")
            if notes:
                entry["notes"] = notes
            entry["updated_at"] = now
            _write_applications(apps)
            return {"updated": entry, "total_tracked": len(apps)}

    entry = {
        "company": company,
        "role": role,
        "url": url,
        "status": status,
        "notes": notes,
        "created_at": now,
        "updated_at": now,
    }
    apps.append(entry)
    _write_applications(apps)
    return {"created": entry, "total_tracked": len(apps)}


def list_applications(status_filter: str | None = None) -> dict[str, Any]:
    """List tracked applications, optionally filtered by status (e.g. 'applied', 'interview')."""
    apps = _load_applications()
    if status_filter:
        apps = [a for a in apps if a.get("status", "").lower() == status_filter.lower()]
    return {"count": len(apps), "applications": apps}


# ---------------------------------------------------------------------------
# Tool registry: specs (for the LLM) + dispatch table (for execution)
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_jobs",
        "description": (
            "Search live job postings by role/keyword across free job boards "
            "(Remotive, Arbeitnow). Use this whenever the user wants to find "
            "open roles matching a title, skill, or keyword."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Role or keyword to search for, e.g. 'backend engineer'."},
                "remote_only": {"type": "boolean", "description": "If true, only return remote-eligible roles."},
                "max_results": {"type": "integer", "description": "Max results to return (default 8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_search",
        "description": "General web search -- use for researching a company, interview process, or salary range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {"type": "integer", "description": "Max results to return (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "track_application",
        "description": (
            "Save or update a job application in the tracker. Call this whenever "
            "the user says they applied to, saved, heard back from, or interviewed "
            "at a company for a role."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "url": {"type": "string", "description": "Job posting URL, if known."},
                "status": {
                    "type": "string",
                    "description": "One of: saved, applied, interview, offer, rejected.",
                },
                "notes": {"type": "string", "description": "Any extra notes."},
            },
            "required": ["company", "role"],
        },
    },
    {
        "name": "list_applications",
        "description": "List all tracked job applications, optionally filtered by status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {"type": "string", "description": "Optional status to filter by."},
            },
        },
    },
]

TOOL_DISPATCH = {
    "search_jobs": search_jobs,
    "web_search": web_search,
    "track_application": track_application,
    "list_applications": list_applications,
}


def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Execute a tool by name with the given input, returning a JSON-safe result."""
    if name not in TOOL_DISPATCH:
        return {"error": f"unknown tool: {name}"}
    try:
        return TOOL_DISPATCH[name](**tool_input)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
