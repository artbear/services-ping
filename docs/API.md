# API contract v1 (implementation handoff)

All paths same origin, UTF-8 JSON. Bind IPv4 127.0.0.1 only. No remote CORS.
GET /api/state: snapshot below. GET /api/config: config below.
GET /api/session: {"csrf_token":"..."}. Mutations require X-CSRF-Token plus matching
Origin if supplied and a strictly local Host header (127.0.0.1/localhost with actual port).
PUT /api/config: full config, validates and saves atomically; returns config.
POST /api/check: {} schedules immediate connectivity and enabled targets; returns {"ok":true}.
POST /api/pause: {"paused":true|false}; returns {"paused":...}.
GET /api/history?target_id=ID&limit=60: {"samples":[sample,...]} chronological.
GET /api/events?limit=50: {"events":[{"timestamp":unix_seconds,"subject":"internet|target_id","previous":"...","current":"...","message":"..."},...]}, newest first.
GET /api/export: config downloadable JSON. Import uses PUT /api/config.
Persistence failures return 500 without applying runtime config. Error status 400/403/413/404/405/500 with {"error":"human-readable message"}.

Config (all fields returned, defaults filled):

```json
{"version":1,"settings":{"internet_interval_s":3,"service_interval_s":30,"timeout_s":2,"concurrency":4,"history_limit":300,"notifications":false},"connectivity":[{"id":"net-google","name":"Google","url":"https://www.google.com/generate_204","expected_statuses":[204]},{"id":"net-cloudflare","name":"Cloudflare","url":"https://www.cloudflare.com/cdn-cgi/trace","expected_statuses":[200]},{"id":"net-yandex","name":"Яндекс","url":"https://ya.ru/robots.txt","expected_statuses":[200]}],"targets":[{"id":"yandex","name":"Яндекс","group":"Сайты","kind":"http","address":"https://ya.ru/","enabled":true,"interval_s":30,"timeout_s":2,"expected_statuses":[200]}]}
```

Target kind: http|tcp|icmp. TCP address hostname:port (bracket IPv6); ICMP hostname/IP.
IDs unique nonempty safe identifiers, names <=100 chars, address <=2048. HTTP only http/https,
no credentials. No shell commands, tokens or arbitrary headers. HTTP expected statuses can be
empty to indicate reachability-only (ordinary HTTP response => reachable, health unknown; 401/403/429/5xx retain special status).
Bounds: internet interval 1..60 s, service/target interval 3..3600 s, timeout .2..15 s,
concurrency 1..16, history 20..2000; at most 100 targets and 10 connectivity references.
At least 2 independent configured connectivity URLs. Defaults include public LLM reachability
targets; application-level API health is not inferred.

Snapshot:

```json
{"timestamp":1700000000,"config_revision":1,"paused":false,"internet":{"status":"checking","latency_ms":null,"checked_at":null,"message":"Проверяем интернет","references":[]},"targets":[{"id":"yandex","name":"Яндекс","group":"Сайты","kind":"http","address":"https://ya.ru/","enabled":true,"in_flight":false,"stale":true,"last_result":null,"status":"pending","latency_ms":null,"checked_at":null,"message":"Ещё не проверен","http_status":null,"reachable":false,"healthy":null}],"settings":{"internet_interval_s":3,"service_interval_s":30,"timeout_s":2,"concurrency":4,"history_limit":300,"notifications":false}}
```

Internet status checking|online|partial|offline; reference objects id,name,url,status,latency_ms,
checked_at,message,http_status. Offline message must explain failed references do not prove
physical link down (can also be DNS/firewall); UI may say «Интернет недоступен по проверкам».
Service status pending|up|reachable|down|blocked|rate_limited|error|unsupported|no_internet|disabled.
401/403 => blocked (reachable true, healthy false); 429 => rate_limited; 5xx => down.
Custom expected statuses influence health but blocked/rate_limit remain explicit.
History sample timestamp,status,latency_ms,http_status,message. Keep actual checked_at when
services are skipped offline, don't fabricate latency measurements.

Frontend owns services_ping/static/{index.html,app.js,style.css}, backend owns Python and tests.
No external assets/network CDNs needed to render. Poll state 1 s while visible, 5 s hidden;
UI disconnect from local monitor must be distinct from internet offline.
Browser notifications opt-in by button with permission handling; in-page banners always available.
Backend optional Android termux-notification opt-in settings.notifications for state changes.
Pause stops new checks and shows remaining in-flight results as such; resume checks immediately.

Clarifications from independent plan review:
- Targets include stale:boolean and last_result: sample|null (last actual measurement),
  allowing no_internet to retain the time/result without displaying a fresh success.
- Manual check during pause performs ONE complete cycle while paused remains true.
- Dedicated bounded connectivity pool (max 4); settings.concurrency bounds service pool.
- Whole probe subprocess wall-clock timeout includes DNS/TLS; disable environment proxies.
- Ignore old results after config revision/removal/offline transition; no unbounded queues.
- No redirects for connectivity. Require exact expected_statuses. Targets may follow bounded
  redirects; whole subprocess deadline always applies. Event retention max 1000.
- Only one internet offline event, no synthetic per-service failure notifications while offline.
- Connectivity status measures reference reachability, not physical interface state.

Final normative decisions (2026-10-07):
- Default Yandex reference uses https://ya.ru/robots.txt (200); root / redirects (302)
  in the live environment, which would otherwise produce permanent diagnostic partial.
- Connectivity online = all exact expected codes, partial = any HTTP response but not all
  expected, offline = zero HTTP responses; no hysteresis in v1 for fast detection.
  3xx/403 reference => partial/reachable, not offline. Explain diagnostic uncertainty in UI.
- Snapshot config_revision:int and targets in_flight:boolean. history service-target-only.
- settings.notifications controls ONLY optional backend Termux notifications; browser opt-in
  is separate browser-local preference. Notify global offline/recovery only, cooldown >=10s.
- HTTP GET, latency DNS through response headers, do not download body. Follow at most
  5 redirects for targets, zero for references. TLS verification always on.
- Unexpected HTTP code => down,true,false; network error/deadline => down,false,false;
  unavailable ping => unsupported,false,null; successful ping with unparseable RTT
  => unsupported,true,null. 401/403/429/5xx priority as above.
- Fixed-delay scheduler (after completion), coalesced manual rerun at most one. Mutation
  config database transaction before in-memory revision. Retention per target; events1000.
- Body limit128KiB, line8KiB, headers32KiB, bounded HTTP workers; static fixed allowlist.
- Missing Origin allowed with token (CLI); Origin:null rejected. CSP self, no framing,
  nosniff. Import UI displays explicit confirmation before applying full config.
