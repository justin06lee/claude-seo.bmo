"""Tests for the cross-site portfolio aggregator (portfolio_report.py)."""
import importlib
import json
import os
import sys
from xml.dom import minidom

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "seo", "scripts")
sys.path.insert(0, _SCRIPTS)

portfolio = importlib.import_module("portfolio_report")


def write_audit(tmp_path, domain, health, findings, business_type=None):
    """Create a {domain}-audit/audit-data.json fixture and return its directory."""
    d = tmp_path / f"{domain}-audit"
    d.mkdir()
    categories = {}
    for title, severity, category in findings:
        categories.setdefault(category, []).append(
            {"title": title, "severity": severity, "description": "x", "recommendation": "y"}
        )
    envelope = {
        "summary": {"health_score": health, "business_type": business_type},
        "categories": [{"name": name, "score": 70, "findings": fs} for name, fs in categories.items()],
    }
    (d / "audit-data.json").write_text(json.dumps(envelope))
    return str(d)


def test_load_audit_counts_severities_and_derives_domain(tmp_path):
    d = write_audit(tmp_path, "example.com", 62, [
        ("Missing canonical", "Critical", "Technical SEO"),
        ("Thin content", "High", "Content"),
        ("Alt text gaps", "Low", "Images"),
    ], business_type="SaaS")
    site = portfolio.load_audit(os.path.join(d, "audit-data.json"))
    assert site["domain"] == "example.com"
    assert site["business_type"] == "SaaS"
    assert site["health_score"] == 62
    assert site["severity_counts"]["critical"] == 1
    assert site["severity_counts"]["high"] == 1
    assert site["severity_counts"]["low"] == 1
    # Actionable = critical + high + medium.
    assert site["actionable_issues"] == 2
    assert site["total_findings"] == 3


def test_unknown_severity_folds_to_info_not_dropped(tmp_path):
    d = write_audit(tmp_path, "a.com", 80, [("Weird", "banana", "Misc")])
    site = portfolio.load_audit(os.path.join(d, "audit-data.json"))
    assert site["severity_counts"]["info"] == 1
    assert site["total_findings"] == 1


def test_synonym_severities_map(tmp_path):
    d = write_audit(tmp_path, "a.com", 80, [
        ("W", "warning", "M"), ("E", "error", "M"),
    ])
    site = portfolio.load_audit(os.path.join(d, "audit-data.json"))
    assert site["severity_counts"]["medium"] == 1  # warning -> medium
    assert site["severity_counts"]["high"] == 1     # error -> high


def test_missing_health_score_is_none_not_zero(tmp_path):
    d = tmp_path / "x-audit"
    d.mkdir()
    (d / "audit-data.json").write_text(json.dumps({"summary": {}, "categories": []}))
    site = portfolio.load_audit(str(d / "audit-data.json"))
    assert site["health_score"] is None


def test_build_ranks_worst_first_and_averages(tmp_path):
    a = write_audit(tmp_path, "good.com", 92, [])
    b = write_audit(tmp_path, "bad.com", 40, [("X", "Critical", "T")])
    c = write_audit(tmp_path, "mid.com", 70, [])
    sites = [portfolio.load_audit(os.path.join(p, "audit-data.json")) for p in (a, b, c)]
    result = portfolio.build_portfolio(sites, None)
    assert [s["domain"] for s in result["sites"]] == ["bad.com", "mid.com", "good.com"]
    assert result["average_health_score"] == round((92 + 40 + 70) / 3)
    assert result["severity_totals"]["critical"] == 1


def test_unscored_sites_rank_last(tmp_path):
    a = write_audit(tmp_path, "scored.com", 30, [])
    d = tmp_path / "unscored-audit"
    d.mkdir()
    (d / "audit-data.json").write_text(json.dumps({"summary": {}, "categories": []}))
    sites = [portfolio.load_audit(str(d / "audit-data.json")),
             portfolio.load_audit(os.path.join(a, "audit-data.json"))]
    result = portfolio.build_portfolio(sites, None)
    assert result["sites"][-1]["domain"] == "unscored"


def test_shared_issues_surface_across_sites(tmp_path):
    a = write_audit(tmp_path, "a.com", 60, [
        ("Missing canonical tag", "Critical", "Technical"),
        ("Site-specific thing", "Low", "Misc"),
    ])
    b = write_audit(tmp_path, "b.com", 55, [
        ("missing canonical TAG", "Critical", "Technical"),  # same issue, different case
    ])
    c = write_audit(tmp_path, "c.com", 90, [
        ("Missing canonical tag", "High", "Technical"),
    ])
    sites = [portfolio.load_audit(os.path.join(p, "audit-data.json")) for p in (a, b, c)]
    result = portfolio.build_portfolio(sites, None)
    shared = result["shared_issues"]
    assert len(shared) == 1
    issue = shared[0]
    assert issue["title"].lower().startswith("missing canonical")
    assert sorted(issue["sites"]) == ["a.com", "b.com", "c.com"]
    # Most severe label across sites wins.
    assert issue["severity"] == "critical"


def test_shared_issue_counts_each_site_once(tmp_path):
    # A site listing the same issue twice must not inflate the reach count.
    a = write_audit(tmp_path, "a.com", 60, [
        ("Duplicate title", "Medium", "Cat1"),
        ("Duplicate title", "Medium", "Cat2"),
    ])
    b = write_audit(tmp_path, "b.com", 60, [("Duplicate title", "Medium", "Cat1")])
    sites = [portfolio.load_audit(os.path.join(p, "audit-data.json")) for p in (a, b)]
    result = portfolio.build_portfolio(sites, None)
    assert result["shared_issues"][0]["sites"] == ["a.com", "b.com"]


def test_deltas_against_previous_snapshot(tmp_path):
    a = write_audit(tmp_path, "a.com", 75, [])
    sites = [portfolio.load_audit(os.path.join(a, "audit-data.json"))]
    previous = {"sites": [{"domain": "a.com", "health_score": 60}]}
    result = portfolio.build_portfolio(sites, previous)
    assert result["sites"][0]["score_delta"] == 15


def test_delta_none_when_site_is_new(tmp_path):
    a = write_audit(tmp_path, "new.com", 75, [])
    sites = [portfolio.load_audit(os.path.join(a, "audit-data.json"))]
    result = portfolio.build_portfolio(sites, {"sites": [{"domain": "old.com", "health_score": 60}]})
    assert result["sites"][0]["score_delta"] is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_scan_finds_audit_dirs(tmp_path):
    write_audit(tmp_path, "a.com", 50, [])
    write_audit(tmp_path, "b.com", 60, [])
    (tmp_path / "not-an-audit").mkdir()
    found = portfolio.discover_inputs([], str(tmp_path))
    assert len(found) == 2
    assert all(f.endswith("audit-data.json") for f in found)


def test_discover_explicit_dir_without_envelope_errors(tmp_path):
    (tmp_path / "empty-audit").mkdir()
    with pytest.raises(portfolio.PortfolioError):
        portfolio.discover_inputs([str(tmp_path / "empty-audit")], None)


def test_discover_dedups(tmp_path):
    d = write_audit(tmp_path, "a.com", 50, [])
    found = portfolio.discover_inputs([d, os.path.join(d, "audit-data.json")], None)
    assert len(found) == 1


# ---------------------------------------------------------------------------
# HTML + CLI
# ---------------------------------------------------------------------------

def test_render_html_is_self_contained_and_names_sites(tmp_path):
    # A shared issue's title appears in the cross-site table; per-site finding
    # titles are intentionally not enumerated (that is the per-site report's job).
    a = write_audit(tmp_path, "alpha.com", 40, [("Missing canonical", "Critical", "T")])
    b = write_audit(tmp_path, "beta.com", 88, [("Missing canonical", "High", "T")])
    sites = [portfolio.load_audit(os.path.join(p, "audit-data.json")) for p in (a, b)]
    result = portfolio.build_portfolio(sites, None)
    html_doc = portfolio.render_html(result)
    assert "<!DOCTYPE html>" in html_doc
    assert "http" not in html_doc.split("<style>")[1].split("</style>")[0]  # no external asset URLs in CSS
    assert "alpha.com" in html_doc and "beta.com" in html_doc
    assert "Missing canonical" in html_doc  # surfaced as a shared issue


def test_render_html_escapes_finding_titles(tmp_path):
    a = write_audit(tmp_path, "a.com", 50, [('Title <b>& "bad"</b>', "High", "T")])
    b = write_audit(tmp_path, "b.com", 50, [('Title <b>& "bad"</b>', "High", "T")])
    sites = [portfolio.load_audit(os.path.join(p, "audit-data.json")) for p in (a, b)]
    result = portfolio.build_portfolio(sites, None)
    html_doc = portfolio.render_html(result)
    assert "<b>&" not in html_doc  # raw markup must be escaped
    assert "&lt;b&gt;" in html_doc


def test_main_writes_html_and_json(tmp_path):
    write_audit(tmp_path, "a.com", 50, [("X", "Critical", "T")])
    write_audit(tmp_path, "b.com", 80, [])
    out = tmp_path / "portfolio.html"
    code = portfolio.main(["--scan", str(tmp_path), "--output", str(out)])
    assert code == 0
    assert out.exists()
    js = tmp_path / "portfolio.json"
    assert js.exists()
    data = json.loads(js.read_text())
    assert data["site_count"] == 2
    assert data["sites"][0]["domain"] == "a.com"  # worst first


def test_main_delta_roundtrip(tmp_path):
    write_audit(tmp_path, "a.com", 60, [])
    out1 = tmp_path / "p1.html"
    js1 = tmp_path / "p1.json"
    portfolio.main(["--scan", str(tmp_path), "--output", str(out1), "--json", str(js1)])
    # Re-audit the same site with a better score.
    import shutil
    shutil.rmtree(tmp_path / "a.com-audit")
    write_audit(tmp_path, "a.com", 78, [])
    out2 = tmp_path / "p2.html"
    js2 = tmp_path / "p2.json"
    portfolio.main(["--scan", str(tmp_path), "--output", str(out2),
                    "--json", str(js2), "--previous", str(js1)])
    data = json.loads(js2.read_text())
    assert data["sites"][0]["score_delta"] == 18


def test_main_no_inputs_is_error(tmp_path):
    assert portfolio.main(["--output", str(tmp_path / "p.html")]) == 2


def test_main_bad_json_is_error(tmp_path):
    d = tmp_path / "broken-audit"
    d.mkdir()
    (d / "audit-data.json").write_text("{not json")
    assert portfolio.main([str(d), "--output", str(tmp_path / "p.html")]) == 2
