"""Attach redacted browser evidence to failures without retrying an action."""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Sample while fixtures are still alive; preserve the original failure."""
    outcome = yield
    report = outcome.get_result()
    evidence = getattr(item, '_browser_evidence', None)
    if report.failed and evidence is not None:
        try:
            detail = evidence.report()
        except Exception:
            detail = 'Evidence collection failed; original test failure is unchanged.'
        report.sections.append(('Redacted browser/server evidence', detail))
