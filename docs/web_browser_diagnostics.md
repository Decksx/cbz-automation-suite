# Windows browser test transport evidence (#207)

The web tests drive the installed Edge, without downloading a browser. CI now
pins Playwright **1.65.1** instead of **1.48.0**. Upstream
[issue 43545](https://github.com/microsoft/playwright/issues/43545) and
[fix 43605](https://github.com/microsoft/playwright/pull/43605) describe Windows
TCP-port randomization failing with WSAENOBUFS/ERR_NO_BUFFER_SPACE even when
ports are available. The newer driver includes that mitigation in its normal
disabled-feature list. We do not replace the list with our own
`--disable-features` flag or change Windows socket settings.

The real installed-browser regression reads effective launch switches through
CDP. It requires one disabled-feature argument, the TCP mitigation on Windows,
the existing HTTP-upgrade setting, and web security remaining enabled. The
fixture explicitly supplies `--enable-automation` for this read-only command;
the earlier driver already supplied that flag by default. Browser assertions
for cookie secrecy, CSRF, Origin isolation, one-use login links, tab isolation
and service-worker isolation remain unchanged.

`browser_evidence.py` observes all pages in the disposable browser contexts.
On a test failure, the web-local pytest hook appends at most 128 classified
response/requestfailed events and the effective-switch summary. Endpoints are
fixed route buckets; arbitrary hosts, paths, query strings and fragments are
discarded. Only known network-error names are retained. Headers, bodies,
cookies, browser keys, login codes and raw command lines are never collected by
this helper. Existing pytest exception traces are not redacted by this hook;
review those privately before publishing any excerpt.

The two smoke-test server fixtures also register their owned child processes.
Their pipes are drained while tests run, avoiding a full diagnostic pipe as a
source of blocking. Reports contain output byte counts, recognised Windows
error numbers and fixed exception names, not raw stdout/stderr. At failure the
hook samples process exit status and makes one unauthenticated loopback health
request with a one-second timeout. A 401 response proves the listener answered;
it does not prove an authenticated operation succeeded. No other browser or
server process is inspected or terminated. Normal fixture teardown still owns
the child process and browser lifetimes.

This is a mitigation and better evidence, **not a retrospective diagnosis**.
PR #206 run 44346736469 failed twice at exact 2596e9f: first a second-tab
navigation returned ERR_NO_BUFFER_SPACE, then a same-origin POST helper raised
`TypeError: Failed to fetch`. Those stages are distinct and their causes remain
unclassified. A later green run does not establish that either failure was
caused by TCP randomization. Keep issue #207 open if further failures need
request/server correlation; do not substitute retries for that investigation.
