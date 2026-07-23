"""Tests for the non-interactive drift runner (drift_ci.py).

The runner is exercised through its own functions with the baseline/compare
engine faked, so no network or SQLite is touched: the value under test is the
orchestration — missing-baseline policy, severity thresholds, aggregation, exit
codes, and the JUnit rendering a CI system consumes.
"""
import importlib
import os
import sys
from pathlib import Path
from xml.dom import minidom

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "seo", "scripts")
sys.path.insert(0, _SCRIPTS)

drift_ci = importlib.import_module("drift_ci")


class FakeEngine:
    """Stand-in for the baseline store and comparison engine.

    baselined URLs have a baseline; compare_results maps a URL to the summary a
    comparison returns. Anything not listed behaves as unreachable.
    """

    def __init__(self, baselined=None, compare_results=None, capture_errors=None):
        self.baselined = set(baselined or [])
        self.compare_results = compare_results or {}
        self.capture_errors = capture_errors or {}
        self.captured = []

    def has_baseline(self, url):
        return url in self.baselined

    def capture(self, url, skip_cwv=False):
        self.captured.append(url)
        if url in self.capture_errors:
            return {"error": self.capture_errors[url]}
        self.baselined.add(url)
        return {"status": "ok", "baseline_id": 1}

    def compare(self, url, skip_cwv=False):
        if url not in self.compare_results:
            return {"error": f"unreachable: {url}"}
        summary = self.compare_results[url]
        findings = []
        for sev in ("critical", "warning", "info"):
            for i in range(summary.get(sev, 0)):
                findings.append({"rule": f"{sev}_rule_{i}", "severity": sev.upper(),
                                 "message": f"{sev} finding {i}", "triggered": True})
        return {
            "baseline_id": 7,
            "baseline_timestamp": "2026-01-01T00:00:00+00:00",
            "summary": {"critical": summary.get("critical", 0),
                        "warning": summary.get("warning", 0),
                        "info": summary.get("info", 0)},
            "triggered_findings": findings,
        }


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(drift_ci, "_has_baseline", fake.has_baseline)
    monkeypatch.setattr(drift_ci, "capture_baseline", fake.capture)
    monkeypatch.setattr(drift_ci, "run_comparison", fake.compare)
    return fake


def test_first_run_seeds_baselines_and_stays_clean(engine):
    report = drift_ci.run("check", ["https://a.test", "https://b.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_CLEAN
    assert report["totals"]["baselined"] == 2
    assert not report["regressions"]
    assert sorted(engine.captured) == ["https://a.test", "https://b.test"]
    assert all(r["outcome"] == "baselined" for r in report["results"])


def test_critical_finding_is_a_regression(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"critical": 1}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_REGRESSION
    assert report["totals"]["critical"] == 1
    assert report["results"][0]["breached"] is True


def test_warning_below_threshold_is_clean(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"warning": 3}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_CLEAN
    assert report["totals"]["warning"] == 3
    assert report["results"][0]["breached"] is False


def test_fail_on_warning_catches_warnings(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"warning": 1}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="warning")
    assert report["exit_code"] == drift_ci.EXIT_REGRESSION


def test_fail_on_none_never_regresses_but_still_counts(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"critical": 5}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="none")
    assert report["exit_code"] == drift_ci.EXIT_CLEAN
    assert report["totals"]["critical"] == 5
    assert report["results"][0]["breached"] is False


def test_fail_on_any_catches_info(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"info": 1}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="any")
    assert report["exit_code"] == drift_ci.EXIT_REGRESSION


def test_on_missing_fail_is_operational(engine):
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="fail", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_OPERATIONAL
    assert report["results"][0]["outcome"] == "missing"
    assert engine.captured == []


def test_on_missing_skip_leaves_it_alone(engine):
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="skip", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_CLEAN
    assert report["results"][0]["outcome"] == "skipped"
    assert report["totals"]["skipped"] == 1
    assert engine.captured == []


def test_unreachable_page_is_operational_not_a_regression(engine):
    engine.baselined = {"https://a.test"}
    # No compare_results entry -> the fake returns an error, as a dead URL would.
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_OPERATIONAL
    assert report["totals"]["errors"] == 1
    assert report["results"][0]["outcome"] == "error"


def test_regression_and_error_together_report_operational(engine):
    # An operational error outranks a regression: a failed run should be fixed
    # before its findings are trusted.
    # Both have baselines; dead.test has no compare_results, so its comparison
    # fails as an unreachable page would.
    engine.baselined = {"https://ok.test", "https://dead.test"}
    engine.compare_results = {"https://ok.test": {"critical": 1}}
    report = drift_ci.run("check", ["https://ok.test", "https://dead.test"],
                          skip_cwv=True, on_missing="skip", fail_on="critical")
    assert report["totals"]["regressions"] == 1
    assert report["totals"]["errors"] == 1
    assert report["exit_code"] == drift_ci.EXIT_OPERATIONAL


def test_baseline_mode_recaptures_every_url(engine):
    engine.baselined = {"https://a.test"}  # already has one; baseline mode refreshes anyway
    report = drift_ci.run("baseline", ["https://a.test", "https://b.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_CLEAN
    assert report["totals"]["baselined"] == 2
    assert sorted(engine.captured) == ["https://a.test", "https://b.test"]


def test_capture_error_during_seed_is_operational(engine):
    engine.capture_errors = {"https://a.test": "SSRF: private address"}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    assert report["exit_code"] == drift_ci.EXIT_OPERATIONAL
    assert report["results"][0]["outcome"] == "error"


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------

def test_load_urls_merges_and_dedups(tmp_path):
    cfg = tmp_path / "urls.json"
    cfg.write_text('{"urls": ["https://a.test", "https://b.test", "https://a.test"]}')
    urls = drift_ci.load_urls(str(cfg), ["https://c.test", "https://a.test"])
    # --url values come first, then config, de-duplicated, order preserved.
    assert urls == ["https://c.test", "https://a.test", "https://b.test"]


def test_load_urls_text_with_comments(tmp_path):
    cfg = tmp_path / "urls.txt"
    cfg.write_text("# watched pages\nhttps://a.test\n\n  https://b.test  \n# trailing\n")
    assert drift_ci.load_urls(str(cfg), []) == ["https://a.test", "https://b.test"]


def test_load_urls_bare_json_list(tmp_path):
    cfg = tmp_path / "urls.json"
    cfg.write_text('["https://a.test", "https://b.test"]')
    assert drift_ci.load_urls(str(cfg), []) == ["https://a.test", "https://b.test"]


def test_load_urls_rejects_bad_json(tmp_path):
    cfg = tmp_path / "urls.json"
    cfg.write_text('{"urls": [ }')
    with pytest.raises(drift_ci.ConfigError):
        drift_ci.load_urls(str(cfg), [])


def test_load_urls_rejects_wrong_shape(tmp_path):
    cfg = tmp_path / "urls.json"
    cfg.write_text('{"urls": "not-a-list"}')
    with pytest.raises(drift_ci.ConfigError):
        drift_ci.load_urls(str(cfg), [])


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(drift_ci.ConfigError):
        drift_ci.load_urls(str(tmp_path / "nope.json"), [])


# ---------------------------------------------------------------------------
# JUnit rendering
# ---------------------------------------------------------------------------

def test_junit_is_well_formed_and_marks_failures(engine):
    # dead.test has a baseline but no compare_results, so it errors like an
    # unreachable page rather than being skipped for want of a baseline.
    engine.baselined = {"https://ok.test", "https://bad.test", "https://dead.test"}
    engine.compare_results = {
        "https://ok.test": {},
        "https://bad.test": {"critical": 1, "warning": 2},
    }
    report = drift_ci.run("check", ["https://ok.test", "https://bad.test", "https://dead.test"],
                          skip_cwv=True, on_missing="skip", fail_on="critical")
    xml = drift_ci.to_junit(report)
    dom = minidom.parseString(xml)  # raises if malformed
    suite = dom.getElementsByTagName("testsuite")[0]
    assert suite.getAttribute("tests") == "3"
    assert suite.getAttribute("failures") == "1"
    assert suite.getAttribute("errors") == "1"
    cases = {c.getAttribute("name"): c for c in dom.getElementsByTagName("testcase")}
    assert cases["https://bad.test"].getElementsByTagName("failure")
    assert cases["https://dead.test"].getElementsByTagName("error")
    assert not cases["https://ok.test"].getElementsByTagName("failure")


def test_junit_escapes_findings(engine):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"critical": 1}}
    report = drift_ci.run("check", ["https://a.test"],
                          skip_cwv=True, on_missing="baseline", fail_on="critical")
    # Inject a metacharacter-laden message and confirm the XML still parses.
    report["results"][0]["findings"][0]["message"] = 'title <changed> & "broken"'
    minidom.parseString(drift_ci.to_junit(report))


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_main_writes_reports_and_returns_exit_code(engine, tmp_path, capsys):
    engine.baselined = {"https://a.test"}
    engine.compare_results = {"https://a.test": {"critical": 1}}
    out = tmp_path / "report.json"
    junit = tmp_path / "report.xml"
    code = drift_ci.main([
        "check", "--url", "https://a.test",
        "--output", str(out), "--junit", str(junit), "--quiet",
    ])
    assert code == drift_ci.EXIT_REGRESSION
    assert out.exists() and junit.exists()
    minidom.parseString(junit.read_text())
    assert '"regressions": true' in out.read_text()


def test_main_no_urls_is_operational(capsys):
    assert drift_ci.main(["check"]) == drift_ci.EXIT_OPERATIONAL
