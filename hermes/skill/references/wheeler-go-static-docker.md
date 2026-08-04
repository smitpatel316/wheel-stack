# Pattern: Updating Go Apps Serving Static From Disk Inside Docker (Wheeler Fix)

Date: 2026-08-02
Issue: Edit host files `~/wheeler/internal/web/static/*` or `templates/*`, deploy nothing changes or 403.

Go FileServer `http.FileServer(http.Dir("internal/web/static"))` reads from container filesystem `/app/internal/web/static`, not host bind (only `/app/data` is bind-mounted). Image layers baked at `docker build` time.

## Steps to Deploy Host Edits to Running Container

```bash
# 1. Copy new files from host into container
sg docker -c "docker cp /home/smitpatel316/wheeler/internal/web/static/css/mobile.css wheeler:/app/internal/web/static/css/mobile.css
docker cp /home/smitpatel316/wheeler/internal/web/static/js/navigation.js wheeler:/app/internal/web/static/js/navigation.js
docker cp /home/smitpatel316/wheeler/internal/web/templates/_navigation.html wheeler:/app/internal/web/templates/_navigation.html
docker cp /home/smitpatel316/wheeler/internal/web/templates/dashboard.html wheeler:/app/internal/web/templates/dashboard.html
docker cp /home/smitpatel316/wheeler/internal/web/templates/treasuries.html wheeler:/app/internal/web/templates/treasuries.html"

# 2. docker cp creates file mode 600 owner 1000:1000 → go FileServer runs as appuser can't read → 403 Forbidden
sg docker -c "docker exec wheeler chmod -R a+r /app/internal/web/static /app/internal/web/templates"

# 3. Templates ParseGlob only at server start → need restart, static files serve instantly after chmod
sg docker -c "docker restart wheeler; sleep 4; docker logs wheeler --tail 5"

# 4. Cloudflare caches HTML → bust
curl -s "https://wheel.smitpatel.net/?bust=$(date +%s)" | grep -o 'mobile.css'

# 5. Bake so restart keeps fix
sg docker -c "docker commit wheeler wheeler:pi"
```

Verify:
```
curl -s http://localhost:8096/ | grep -o 'mobile.css|mobile-topbar' → hits
curl -s http://localhost:8096/static/css/mobile.css | wc -c → 9764
curl -sI https://wheel.smitpatel.net/static/css/mobile.css → 200 Content-Type text/css not 403
```

## CGO Build Pattern (go-sqlite3 requires CGO on ARM64 Pi)

Pi builder bug "failed to commit ... snapshot does not exist" on RUN apk add gcc musl-dev when low disk/buildkit 38GB cache.

Reliable method:
```bash
docker run --rm --entrypoint sh -v /home/smitpatel316/wheeler:/work -w /work golang:1.24-alpine \
  -c 'apk add --no-cache gcc musl-dev; CGO_ENABLED=1 go build -o /work/wheeler.new .'
sg docker -c "docker cp /home/smitpatel316/wheeler/wheeler.new wheeler:/app/wheeler.new && docker exec wheeler sh -c 'mv /app/wheeler.new /app/wheeler && chmod +x /app/wheeler && ls -lh /app/wheeler' && docker restart wheeler"
sg docker -c "docker commit wheeler wheeler:pi"
```

Check new binary active via log line "Treasuries (incl SGOV)" vs old "Treasuries: $0.00".

## Mobile CSS Hamburger Pattern

Key DOM:
- `mobile-topbar` fixed 52px top hidden desktop, flex @768px
- `sidebar` fixed transform translateX(-100%) → open 0 drawer 280px transition 0.28s
- `sidebarOverlay` blur dim, closes on click
- JS `initMobileNav()` open/close + ESC + link auto-close @768px
- Must init before collapsible Symbols toggles or event listeners clash
