"""Bounded, credential-free evidence for installed-browser test failures.

This records transport outcomes, not request headers, bodies or raw URLs. Pipe
drainers prevent diagnostic output from filling a child server's stdout/stderr;
only byte counts and recognised error codes/classes can enter a test report.
"""
from collections import deque
import http.client
import json
import os
import re
import threading
from urllib.parse import urlsplit

_NETWORK_ERRORS = frozenset('net::ERR_' + name for name in (
    'NO_BUFFER_SPACE', 'FAILED', 'ABORTED', 'CONNECTION_RESET', 'CONNECTION_CLOSED',
    'CONNECTION_REFUSED', 'TIMED_OUT', 'EMPTY_RESPONSE', 'NETWORK_CHANGED',
    'NAME_NOT_RESOLVED', 'ADDRESS_UNREACHABLE', 'INTERNET_DISCONNECTED',
))


def launch_summary(arguments):
    """Return selected effective switches without profile paths or other args."""
    features = [arg.split('=', 1)[1].split(',') for arg in arguments
                if arg.startswith('--disable-features=')]
    return {
        'feature_arguments': len(features),
        'tcp_randomization_disabled': any('TcpPortRandomizationWin' in row for row in features),
        'https_upgrades_disabled': any('HttpsUpgrades' in row for row in features),
        'web_security_disabled': '--disable-web-security' in arguments,
    }


def _route(url):
    """Classify a URL; arbitrary paths, host labels, query and fragment stay private."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        origin = 'loopback' if host == '127.0.0.1' or host == 'localhost' or host.endswith('.localhost') else 'other'
        path = parsed.path
        if path not in ('/', '/login', '/api/session', '/api/health', '/api/nothing', '/api/jobs'):
            path = '/static/*' if path.startswith('/static/') else 'other'
        return {'origin': origin, 'route': path}
    except ValueError:
        return {'origin': 'other', 'route': 'other'}


class PipeSummary:
    """Count output and retain recognised diagnostics, never raw output text."""
    def __init__(self):
        self.bytes = 0
        self.codes = set()
        self.exceptions = set()
        self._tail = b''
        self._lock = threading.Lock()

    def feed(self, data):
        """Consume a pipe chunk; the small tail allows split error tokens."""
        with self._lock:
            self.bytes += len(data)
            text = (self._tail + data).decode('utf-8', errors='replace')
            self.codes.update(re.findall(r'\[WinError (\d{1,5})\]', text))
            self.exceptions.update(re.findall(r'\b(OSError|BrokenPipeError|ConnectionResetError|TimeoutError)\b', text))
            self._tail = data[-64:]

    def snapshot(self):
        """Return only bounded counters and fixed-vocabulary exception names."""
        with self._lock:
            return {'bytes': self.bytes, 'winerror_codes': sorted(self.codes),
                    'exception_types': sorted(self.exceptions)}


class BrowserEvidence:
    """One test's bounded browser events and owned server process observations."""
    def __init__(self):
        self.events = deque(maxlen=128)
        self.servers = []
        self.browser = {}

    def attach(self, context):
        """Observe requests from every page in this disposable context."""
        context.on('requestfailed', lambda req: self.record('failed', req, req.failure))
        context.on('response', lambda response: self.record('response', response.request, response.status))

    def record(self, kind, request, result):
        """Store a classified endpoint and status/error code, never free text."""
        row = {'kind': kind, **_route(request.url)}
        row['method'] = request.method if request.method in ('GET', 'HEAD', 'POST', 'OPTIONS') else 'other'
        if kind == 'response':
            row['status'] = result if isinstance(result, int) and 100 <= result <= 599 else None
        else:
            row['error'] = result if isinstance(result, str) and result in _NETWORK_ERRORS else 'other'
        self.events.append(row)

    def watch_server(self, process, port):
        """Drain only this test's child pipes after its session has been read."""
        summaries = {}
        for name in ('stdout', 'stderr'):
            summary = summaries[name] = PipeSummary()
            pipe = getattr(process, name)
            def drain(stream=pipe, sink=summary):
                try:
                    while data := os.read(stream.fileno(), 4096):
                        sink.feed(data)
                except (OSError, ValueError):  # fixture teardown closed the owned pipe
                    pass
            threading.Thread(target=drain, daemon=True).start()
        self.servers.append((process, port, summaries))

    def report(self):
        """Take a failure-time liveness sample; do not authenticate or mutate."""
        servers = []
        for process, port, summaries in self.servers:
            row = {'pid': process.pid, 'exit_code': process.poll(),
                   **{name: value.snapshot() for name, value in summaries.items()}}
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', '/api/health')
                response = connection.getresponse()
                row['health_status'] = response.status
                response.read(1024)
            except OSError:
                row['health_status'] = 'unreachable'
            finally:
                connection.close()
            servers.append(row)
        return json.dumps({'browser': self.browser, 'servers': servers, 'events': list(self.events)}, sort_keys=True)


def evidence_for(request):
    """Share evidence between a test's server and browser fixtures."""
    if not hasattr(request.node, '_browser_evidence'):
        request.node._browser_evidence = BrowserEvidence()
    return request.node._browser_evidence
