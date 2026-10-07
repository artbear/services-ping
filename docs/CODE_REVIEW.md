# Independent final code review

Reviewed `docs/PLAN.md`, `docs/API.md`, `docs/ACCEPTANCE.md`, `README.md`, all
`services_ping/*.py`, static assets, and tests. The baseline command
`python3 -m unittest discover -s tests -v` passes all 19 tests. I did not run the
browser smoke because it writes screenshots outside this reviewer's assigned file;
the parent's earlier passing browser run is consistent with the paths it covers.

## Initial release decision (before fixes; superseded below)

**Not ready for release.** Findings P1-1 through P1-5 are release blockers because
they contradict explicit v1 scheduling, persistence, resource-bound, platform, or
truthful-display requirements. P2 findings should also be addressed, but do not by
themselves block the rest of the monitor.

## Release blockers

### P1-1 — A manual check while paused probes only the first concurrency-sized wave

Files: `services_ping/monitor.py:109-128`.

`apply_connectivity(..., force_targets=True)` calls `run_targets(due_only=False)`
once. `run_targets` submits at most `settings.concurrency` targets and has no state
representing the remainder of that manual cycle. While paused, `_schedule` never
calls `run_targets`, so all later targets remain pending indefinitely. This violates
the specified "one complete cycle while remaining paused" behavior. There is also a
small pause race: `_schedule` tests `self.paused` outside the monitor lock and
`run_targets` itself has no pause guard, so an automatic caller can submit new work
after pause has been requested.

Focused repro with three enabled targets, concurrency 1, then `set_paused(True)` and
`check_now()` produced:

```text
target_probe_calls: ['t0']
statuses: [('t0', 'up'), ('t1', 'pending'), ('t2', 'pending')]
still_paused: True
```

Action: represent a manual target cycle explicitly and refill its bounded wave from
completion callbacks until every enabled target in that revision has been attempted
once. Keep at most `concurrency` probes active and coalesce at most one additional
manual cycle. Make automatic target submission recheck pause under the monitor lock,
with an explicit bypass only for the manual cycle. Add a deterministic test with
more targets than concurrency, plus a pause-vs-scheduler race test.

### P1-2 — Config replacement is not atomic when history cleanup fails

Files: `services_ping/monitor.py:61-69`, `services_ping/store.py:29-34,50-54`,
`services_ping/server.py:69-76`.

`replace_config` commits the new config first, publishes it to memory and increments
the revision, and only then starts a separate transaction to delete orphan history.
If that second write fails (locked/full/I/O error), PUT returns 500 even though both
the saved config and runtime config have changed. That directly violates the API
contract that a persistence failure returns 500 without applying runtime config.

Focused repro by making `delete_targets_not_in` raise after `save_config` produced:

```text
raised: cleanup failed
runtime_changed: True
saved_changed: True
revision: 2
```

Action: expose one Store transaction that validates/saves config, deletes orphan
samples, and applies retention pruning; publish the new in-memory revision only after
that transaction commits. Add an API-level failure-injection test for each write in
the transaction and assert response 500, unchanged DB config/history, unchanged
runtime revision, and unchanged snapshot.

Related retention gap: lowering `history_limit` does not prune existing samples
until each target records another result (`Store.sample`, lines 35-38). A disabled
target can therefore remain above the configured limit indefinitely. Prune every
retained target in the same config transaction.

### P1-3 — The HTTP server has unbounded request threads and no stalled-client timeout

Files: `services_ping/server.py:4,13-21,91`.

`ThreadingHTTPServer` creates a thread per accepted connection. There is no semaphore,
bounded worker pool, or socket read timeout. The 8 KiB request-line and 32 KiB header
checks happen inside an already-created worker and therefore do not bound workers or
slow/incomplete requests. This misses the explicit "bounded HTTP workers" protection.

Focused local repro opened 40 connections and sent only an incomplete request line;
after 300 ms it reported:

```text
open_clients: 40 request_threads: 40
```

Closing those clients also caused server-side `BrokenPipeError` tracebacks. Action:
bound accepted/request work (for example, a small semaphore-backed server), set a
short per-connection header/body read timeout, and handle disconnects without noisy
tracebacks. Add a test that opens more stalled clients than the limit and verifies
the worker count remains bounded while normal requests still complete.

### P1-4 — ICMP timeout arguments are wrong on macOS, and successful unparseable replies are reported as down

Files: `services_ping/probe.py:36-43`; affected claim: `README.md` laptop support and
ICMP RTT semantics.

All non-Windows systems receive `ping -W max(1, int(timeout))`. Linux `ping -W` uses
seconds, but macOS `ping -W` uses milliseconds. Thus a normal 2-second target timeout
becomes a 2 ms per-packet wait on macOS, commonly suppressing the reply line and
creating false failures. `os.name` cannot distinguish Linux from macOS.

Separately, when `ping` exits 0 but its localized/platform output does not match
`(?:time|время)[=<]([0-9.]+)`, line 43 returns `down` / "no ICMP reply". The plan
requires `unsupported` when RTT cannot be extracted reliably. A mocked successful
output without a parseable RTT reproduced:

```text
successful-unparseable ping: {'status': 'down', 'latency_ms': None,
  'message': 'no ICMP reply', 'reachable': False, 'healthy': False, ...}
```

The regex also cannot parse decimal commas reliably (it can silently truncate one to
an integer). Action: construct arguments per `sys.platform` (Darwin milliseconds,
Linux seconds, Windows milliseconds), parse only well-defined platform formats, and
return `unsupported`/unknown health when a successful reply cannot yield a reliable
RTT. Add mocked Linux/macOS/Windows argument and output tests; keep the parent helper
deadline as the final wall-clock bound.

### P1-5 — The history graph fabricates 0 ms points for results with no latency

File: `services_ping/static/app.js:261-272`.

`Number(sample.latency_ms)` converts JSON `null` to `0`, and `Number.isFinite(0)` then
keeps the point. A timeout, refused connection, unsupported ping, or other no-latency
sample is consequently drawn as a successful-looking 0 ms measurement even though
the list below correctly renders it as `—`. This is a concrete truthfulness failure.

Exact expression repro:

```text
[{latency_ms:null},{latency_ms:12.4}] ->
[{index:0,value:0},{index:1,value:12.4}]
```

Action: reject `null`/missing latency before numeric conversion, and add browser/unit
coverage with mixed failed/null and successful samples asserting no zero-valued SVG
point is generated for the failed sample.

## Other actionable findings

### P2-1 — Backend notification transitions do not match offline/recovery semantics

File: `services_ping/monitor.py:95-110`.

The notifier fires for any transition whose new status is `online`, including initial
`checking -> online` and `partial -> online`, but it does not fire for the genuine
recovery `offline -> partial`. This is inconsistent with both the browser notifier
and the requirement to notify global offline/recovery only.

With cooldown reset between transitions, a focused notifier spy observed:

```text
initial_online_notifications: ['online']
offline_notifications: ['offline']
offline_to_partial_notifications: []
partial_to_online_notifications: ['online']
```

Action: derive notification intent from `(previous, status)`: notify entering
`offline`, and leaving `offline` for either `online` or `partial`; do not notify
initial online or partial-to-online. Add transition-table tests including cooldown.

### P2-2 — Endpoint validation accepts malformed HTTP and TCP addresses

File: `services_ping/config.py:50-59`.

Validation checks only the URL scheme/hostname and a final numeric TCP port. It does
not access `urlsplit(...).port` (which detects an invalid HTTP port), reject TCP
whitespace, or validate bracket structure/nonempty host. Focused calls to
`validate_config` accepted all of these:

```text
https://example.test:notaport/  (HTTP)
[]:80                           (TCP)
host name:80                    (TCP)
```

They can be imported/saved successfully and then fail forever as network errors
instead of being rejected as malformed configuration. Action: validate parsed HTTP
ports and enforce a precise `host:port` / `[IPv6]:port` TCP grammar with no whitespace;
add API import tests for malformed ports and brackets.

## Areas reviewed with no additional blocker found

- Connectivity/service pools are separately bounded; connectivity generations and
  target config/network generations reject stale results, and same-ID in-flight work
  is retained across config replacement to prevent overlap.
- HTTP probe subprocess deadlines cover DNS/TCP/TLS/redirect work; environment proxy
  variables are removed; connectivity redirects are not followed; response bodies
  are not downloaded.
- HTTP status priority (401/403, 429, 5xx, expected-code mismatch) matches the contract.
- SQLite sample insertion plus per-target deletion and event insertion plus 1000-row
  deletion are individually transactional and serialized. The cross-operation config
  transaction and immediate limit reduction remain the exception described above.
- Host, Origin, CSRF, fixed static allowlist, mutation body size, request-line/header
  checks, CSP, `nosniff`, and frontend `textContent` use are otherwise aligned with the
  stated localhost threat model.
- Browser CRUD/settings/import confirmation/export/state disconnect handling are
  coherent in the reviewed code; the existing smoke test covers their main happy
  paths, but not the null-latency graph or notifier transition table.

## Required verification after fixes

Re-run the 19 Python tests and browser smoke, then add focused regression coverage for
all P1 repros above. In particular, test a paused manual cycle with target count greater
than concurrency, atomic PUT failure after the config write has begun, stalled HTTP
connections beyond the worker cap, mocked Darwin ICMP arguments/output, and history
containing `latency_ms: null`. No public network is needed for any of these tests.

## Final verification after fixes (2026-10-07)

**Code-review gate: PASS. No remaining release blockers.** The initial P1/P2
decision above is retained as review history but is fully superseded by this verdict.

The final implementation and regression coverage were re-read for every finding:

- **P1-1 resolved:** paused manual checks maintain a bounded set of remaining target
  IDs and refill batches from completion callbacks until every enabled target is
  visited. Automatic connectivity and target submission now recheck pause under the
  monitor lock. The concurrency-1/multiple-target paused-cycle regression passes.
- **P1-2 resolved:** config update, orphan cleanup, and per-target retention pruning
  execute in one SQLite transaction before runtime publication. Failure injection
  after orphan deletion and, separately, after an executed retention `DELETE` rolls
  back config, cleanup, retained samples, and leaves the runtime snapshot/revision
  unchanged. Lowering retention also prunes disabled targets immediately.
- **P1-3 resolved:** the loopback server uses a 16-slot bounded semaphore, rejects
  excess connections, applies a two-second socket idle timeout, releases slots in a
  `finally` path, and suppresses expected disconnect/timeout tracebacks. The stalled
  client regression confirms the worker count stays bounded and the API recovers.
- **P1-4 resolved:** ICMP timeout arguments are selected per Linux/macOS/Windows unit
  semantics; RTT parsing handles decimal comma/dot, and a successful reply with an
  unknown RTT format is explicitly `unsupported,true,null`. The API contract now
  distinguishes that case from an unavailable ping command (`unsupported,false,null`).
- **P1-5 resolved:** null or missing latency is converted to `NaN` and excluded before
  SVG points are built. The updated browser smoke asserts that mixed null/missing/real
  samples create exactly one point for the real 7.4 ms measurement.
- **P2-1 resolved:** backend notifications use an explicit transition predicate:
  entering offline and recovery from offline to online/partial only, with cooldown.
- **P2-2 resolved:** HTTP ports/credentials/host syntax and TCP hostname/bracketed-IPv6
  syntax are validated before persistence; the previously reproduced malformed
  addresses are rejected by regression tests.

Verification evidence:

```text
python3 -m unittest discover -s tests -v
Ran 28 tests in 13.197s
OK

node --check services_ping/static/app.js
git diff --check -- services_ping tests docs/CODE_REVIEW.md
# both completed successfully with no output
```

The parent independently reported the updated Playwright smoke passing on desktop and
390 px mobile viewports, including the new null/missing RTT graph assertion. I did not
rerun that harness in this review thread because it writes screenshot artifacts outside
this reviewer's assigned file.

No new correctness, concurrency, persistence, probe-platform, localhost-security, or
browser-truthfulness blocker was found in the final sources. Physical macOS/Windows
ICMP behavior and physical Android background behavior remain platform verification
limits, not hidden release claims; the implementation has deterministic platform
adapter coverage and the README continues to label Android as a Termux preview.
