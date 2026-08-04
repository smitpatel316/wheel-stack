# Mobile v5 — Cloudflare + Permissions + Cache-Bust — Session 2026-08-03 Night

## Incident
User reporting: Dividends page screenshot 589×1280 JPEG (50KB) still broken after v4 claimed fixed. Vision API 401 fail. Investigation found TWO hidden blockers:

### 1. File permissions 0600 → 403 Forbidden for static CSS
- `docker cp host.css container:/app/.../mobile.css` creates file as `0600 1000:1000` (host UID)
- Container runs as `appuser`, not root, cannot read 600 → Go `FileServer` returns 403, body 14 bytes "403 Forbidden"
- Symptom: `curl -s http://localhost:8096/static/css/mobile.css | wc -c` → 14 not 7607
- But `https://wheel.smitpatel.net/static/css/mobile.css` via tunnel returned 7607 (because tunnel container? Actually CF cached earlier). Confusing dual path.
- Fix: `docker exec wheeler sh -c "chown -R appuser:appuser /app/internal/web/static; chmod -R a+r /app/internal/web/static; ls -l ..."` after every cp. Sh command needed because glob `*` fails via plain docker exec (shell expansion).
- Diagnostic commands:
```bash
docker exec wheeler sh -c "ls -l /app/internal/web/static/css/mobile.css; id"
curl -s http://localhost:8096/static/css/mobile.css | wc -c # should be 7607 not 14
curl -s http://localhost:8096/static/css/mobile.css | head -1 # /* MOBILE v4...
```

### 2. Cloudflare cache HIT stale 19506 vs new 7607
- `curl -sI https://wheel.smitpatel.net/static/css/mobile.css | grep -i cache` → `cf-cache-status: HIT max-age=14400`
- Host v4 7607, CF serving old v3 19506 bytes (4h TTL)
- Tunnel `wheel.smitpatel.net` goes via Cloudflare Tunnel (cloudflared) but static assets still cached at edge due to cache-control header? Server sets no explicit no-cache.
- Fix: Versioned query string cache-bust:
```bash
cp mobile.css mobile.v4.css
docker cp mobile.css container + mobile.v4.css
sed -i "s|/static/css/mobile.css|/static/css/mobile.css?v=4.1|g" templates/*.html  # 13 files
docker cp templates/*.html container
docker exec chmod...
docker restart + commit
```
- After: `https://wheel.smitpatel.net/dividends?finalv4` includes `mobile.css?v=4.1` → new URL → MISS then new HIT 7607
- Verification: `curl -s https://wheel.smitpatel.net/static/css/mobile.css?v=4.1 | wc -c` → 7607, `curl -s .../mobile.css | wc -c` → still old 19506 HIT until TTL expires. So bump version on each deploy.

### 3. Combined deploy checklist after any UI edit (canonical)
```bash
for f in ~/wheeler/internal/web/templates/*.html; do docker cp "$f" wheeler:/app/internal/web/templates/$(basename "$f"); done
docker cp ~/wheeler/internal/web/static/css/mobile.css wheeler:/app/internal/web/static/css/mobile.css
docker cp ~/wheeler/internal/web/static/js/navigation.js wheeler:/app/internal/web/static/js/
docker exec wheeler sh -c "chmod -R a+r /app/internal/web && chown -R appuser:appuser /app/internal/web/static"
docker restart wheeler; sleep 5; docker logs --tail 3
curl -s http://localhost:8096/static/css/mobile.css | wc -c # 7607
curl -s http://localhost:8096/dividends | grep -o "mobile.css[^\\"]*" # v=4.1
sg docker -c 'docker commit wheeler wheeler:pi'
```

### User-facing workaround
Tell user to open `https://wheel.smitpatel.net/dividends?v=4.1` (query param busts HTML cache too, which may contain old mobile.css href without version). Pull-to-refresh / hard refresh.

### Future prevention
- Add `Cache-Control: no-cache` header for static css? Or always versioned filename mobile.v4.css not query
- In Dockerfile, COPY with correct perms or chown in final layer
- In entry.sh, chmod a+r static dir on startup to self-heal 0600 from cp
- Document v bump required: host file +.html references must align

### Live final state after v4.1
- Origin static 7607 200, public static v=4.1 7607 200 HIT new content, allocation Treasuries $49k intact, 1×496 SGOV open order queued
- Image f441ff04abf9 130MB less than second ago
