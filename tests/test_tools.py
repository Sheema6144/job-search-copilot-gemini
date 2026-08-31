"""
Unit tests for tools.py.

Network-dependent tools (search_jobs, web_search) are tested with mocked
HTTP responses so the suite is fast and deterministic in CI. The
application tracker is tested against a temp file so tests never touch
the real applications.json.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import tools


# ---------------------------------------------------------------------------
# search_jobs
# ---------------------------------------------------------------------------

def _fake_remotive_response(jobs):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"jobs": jobs}
    return resp


def _fake_arbeitnow_response(jobs):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": jobs}
    return resp


@patch("tools.requests.get")
def test_search_jobs_merges_and_dedupes(mock_get):
    remotive_jobs = [
        {
            "title": "Backend Engineer",
            "company_name": "Acme",
            "candidate_required_location": "Worldwide",
            "job_type": "full_time",
            "salary": "",
            "url": "https://remotive.com/job/1",
            "description": "<p>Build APIs</p>",
            "publication_date": "2026-01-01",
        }
    ]
    arbeitnow_jobs = [
        {
            "title": "Backend Engineer",  # duplicate of remotive's, should be deduped
            "company_name": "Acme",
            "location": "Remote",
            "remote": True,
            "job_types": ["Full-time"],
            "url": "https://arbeitnow.com/job/1",
            "description": "<p>Build APIs</p>",
            "created_at": "2026-01-01",
            "tags": ["backend"],
        },
        {
            "title": "Backend Developer",
            "company_name": "Widgets Inc",
            "location": "Remote",
            "remote": True,
            "job_types": ["Full-time"],
            "url": "https://arbeitnow.com/job/2",
            "description": "<p>Ship features</p>",
            "created_at": "2026-01-02",
            "tags": ["backend"],
        },
    ]

    def side_effect(url, *args, **kwargs):
        if "remotive" in url:
            return _fake_remotive_response(remotive_jobs)
        return _fake_arbeitnow_response(arbeitnow_jobs)

    mock_get.side_effect = side_effect

    result = tools.search_jobs("backend", max_results=10)

    assert result["count"] == 2  # Acme entry deduped, Widgets Inc kept
    titles = {j["title"] for j in result["jobs"]}
    assert "Backend Engineer" in titles
    assert "Backend Developer" in titles


@patch("tools.requests.get")
def test_search_jobs_remote_only_filter(mock_get):
    mock_get.side_effect = lambda url, *a, **k: (
        _fake_remotive_response(
            [
                {
                    "title": "Remote Role",
                    "company_name": "A",
                    "candidate_required_location": "Worldwide",
                    "job_type": "full_time",
                    "salary": "",
                    "url": "https://x/1",
                    "description": "",
                    "publication_date": "",
                }
            ]
        )
        if "remotive" in url
        else _fake_arbeitnow_response([])
    )

    result = tools.search_jobs("role", remote_only=True)
    assert all(j["remote"] for j in result["jobs"])


@patch("tools.requests.get", side_effect=tools.requests.RequestException("boom"))
def test_search_jobs_handles_network_failure_gracefully(mock_get):
    result = tools.search_jobs("anything")
    assert result["count"] == 0
    assert result["jobs"] == []


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

@patch("tools.requests.post")
def test_web_search_parses_results(mock_post):
    html = (
        '<a rel="nofollow" class="result__a" href="https://example.com">Example Title</a>'
        '<a class="result__snippet">Example snippet text</a>'
    )
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.text = html
    mock_post.return_value = resp

    result = tools.web_search("example query")
    assert result["query"] == "example query"
    assert result["results"][0]["title"] == "Example Title"
    assert result["results"][0]["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# Application tracker (uses a temp file via monkeypatch)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_applications_file(tmp_path, monkeypatch):
    temp_file = tmp_path / "applications.json"
    monkeypatch.setattr(tools, "APPLICATIONS_FILE", str(temp_file))
    yield temp_file


def test_track_application_creates_new_entry():
    result = tools.track_application("Acme", "Backend Engineer", status="applied")
    assert result["created"]["company"] == "Acme"
    assert result["total_tracked"] == 1


def test_track_application_updates_existing_entry_case_insensitively():
    tools.track_application("Acme", "Backend Engineer", status="applied")
    result = tools.track_application("acme", "backend engineer", status="interview")

    assert "updated" in result
    assert result["updated"]["status"] == "interview"
    assert result["total_tracked"] == 1  # no duplicate created


def test_list_applications_filters_by_status():
    tools.track_application("Acme", "Backend Engineer", status="applied")
    tools.track_application("Widgets Inc", "Data Analyst", status="interview")

    only_interview = tools.list_applications(status_filter="interview")
    assert only_interview["count"] == 1
    assert only_interview["applications"][0]["company"] == "Widgets Inc"


# ---------------------------------------------------------------------------
# Tool registry / dispatch
# ---------------------------------------------------------------------------

def test_run_tool_dispatches_known_tool():
    result = tools.run_tool("track_application", {"company": "Acme", "role": "Engineer"})
    assert "created" in result


def test_run_tool_rejects_unknown_tool():
    result = tools.run_tool("does_not_exist", {})
    assert "error" in result


def test_run_tool_reports_bad_arguments():
    result = tools.run_tool("track_application", {"nonexistent_arg": "x"})
    assert "error" in result


def test_all_tool_specs_have_matching_dispatch_entry():
    spec_names = {spec["name"] for spec in tools.TOOL_SPECS}
    dispatch_names = set(tools.TOOL_DISPATCH.keys())
    assert spec_names == dispatch_names
