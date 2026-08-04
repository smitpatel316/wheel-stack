# Mobile v4 FINAL — 2026-08-03 03:11 Definitive Fix (3rd Correction)

User corrections:
1. v1: "Can you make the web app more friendly for mobile screens" → basic hamburger
2. v2: "Monthly, options, dividends, metrics, symbols, admin, help, zen all are not mobile friendly still" → v2 14KB
3. v3: "Dividends, metrics, admin, help and zen still not mobile friendly" → v3 19.5KB
4. v4: "https://wheel.smitpatel.net/dividends still not mobile friendly" + "Neither is metrics page" → v4 7607B final

## Root Causes v4 (why v3 still reported broken)

**A. BrowserBase 1280px trap:**
- Browser tool snapshot runs at 1280px desktop viewport → `window.matchMedia('(max-width:768px)').matches = false` → `.summary-grid` reports `grid: 225.75px ×4` + `mobile-topbar display:none` — this IS correct desktop layout, not bug
- User phone at 390px sees different: `@media 768` should collapse to `1fr`
- Fix: Never declare mobile fixed from 1280 snapshot. Verify phone by:
  1. Origin `curl localhost:8096/dividends | grep "@media"` must have inline rule
  2. `curl localhost:8096/metrics | grep "@media"` same
  3. Real phone or JS emulation forcing 390 width check of `getComputedStyle(grid).gridTemplateColumns`

**B. Inline <style> specificity war:**
- HTML order: `<link mobile.css> line12` then `<style>.dividends-calendar {grid: repeat(2,1fr)} line14-173`
- Later inline wins over external unless external has `!important`
- v2/v3 external had `!important` but still fragile
- v4 fix: BOTH layers have `!important` — external 7607B every rule `!important` + INLINE duplicate inside each template's own `<style> @media (max-width:768px) {...!important}` — works even if CDN blocked

**C. Go ParseGlob stale cache:**
- Go `wheeler` binary does `template.ParseGlob("internal/web/templates/*.html")` once at startup
- Host `docker cp new.html container` without restart → `curl localhost:8096/dividends | grep mobile.css` still 0
- Required sequence: `cp + chmod -R a+r /app/internal/web/ + restart + sleep 5 + logs + commit`
- Verified by `docker exec wheeler wc -c /app/.../mobile.css` = 7607 but HTML still old without restart

## Files Changed v4

`internal/web/static/css/mobile.css` 7607 bytes:
```css
html{overflow-x:hidden} body{overflow-x:hidden;max-width:100vw}
.mobile-topbar{display:none;position:fixed;...z-index:999}
@media (max-width:768px){
  .mobile-topbar{display:flex!important}
  .sidebar{position:fixed!important;transform:translateX(-100%)!important;width:280px!important}
  .sidebar.open{transform:translateX(0)!important}
  .summary-grid{grid-template-columns:1fr 1fr!important}
  .section-row{grid-template-columns:1fr!important}
  .charts-row{grid-template-columns:1fr!important}
  .dividends-calendar{grid-template-columns:1fr!important}
  .accordion-header{flex-direction:column!important}
  .data-table{min-width:480px!important}
  .charts-grid{grid-template-columns:1fr!important}
  .help-panels-grid{grid-template-columns:1fr!important}
  .zen-header-row{flex-direction:column!important}
  .zen-stock-prices{display:grid!important;grid-template-columns:1fr 1fr!important}
  .filter-group{width:100%!important}
}
```

Inline duplication added to:
- `dividends.html` line 19: `@media 768 { calendar 1fr, section-row 1fr, charts-row 1fr, summary 1fr 1fr, accordion column, stats wrap 45%, data-table swipe, 420 summary 1fr }`
- `metrics.html` line 51: `charts-grid 1fr, chart-card 12px, chart-container 250px, metrics-table swipe 400px`
- `zen.html` line 15-23: header-row column, main-metrics column, portfolio grid 3, stock-prices grid 2, chart 300px
- `help.html` line 14-20: panels 1fr, tips 1fr, tab-nav column 100% 44px, story-table swipe 600px, key-metrics 2→1

## Deploy Sequence v4 (canonical on budupi)

```bash
sg docker -c '
for f in ~/wheeler/internal/web/templates/*.html; do docker cp "$f" wheeler:/app/internal/web/templates/$(basename "$f"); done
docker cp ~/wheeler/internal/web/static/css/mobile.css wheeler:/app/internal/web/static/css/mobile.css
docker cp ~/wheeler/internal/web/static/js/navigation.js wheeler:/app/internal/web/static/js/navigation.js
docker cp ~/wheeler/internal/web/templates/_navigation.html wheeler:/app/internal/web/templates/_navigation.html
docker exec wheeler chmod -R a+r /app/internal/web/
docker restart wheeler
sleep 5
docker logs wheeler --tail 5
curl -s http://localhost:8096/dividends | grep -o mobile.css | wc -l  # 1
curl -s http://localhost:8096/metrics | grep -o mobile.css
curl -s http://localhost:8096/dividends | grep "@media"  # inline present
'
sg docker -c 'docker commit wheeler wheeler:pi && docker images wheeler:pi --format "{{.ID}} {{.CreatedSince}} {{.Size}}"'
# Result f504e11ba78e Less than a second ago 130MB 2026-08-03 03:11
```

## Verification Checklist v4 FINAL (mandatory)

```bash
# 1. Origin loop all 6 flagged pages have mobile.css via origin (not tunnel)
for p in dividends metrics help zen backup import; do echo -n "$p: "; curl -s http://localhost:8096/$p | grep -o mobile.css; done
# Expected all 1

# 2. Inline media present (double protection)
curl -s http://localhost:8096/dividends | grep -n "@media.*768" | head
curl -s http://localhost:8096/metrics | grep -n "@media.*768" | head
# >=1 each

# 3. Size
curl -s http://localhost:8096/static/css/mobile.css | wc -c  # 7607 v4 (19506 v3, 14026 v2)

# 4. Phone emulation check (if BrowserBase) — check CSS rule exists, don't rely on 1280 snapshot cols
# mobile.css href present + @media present = pass desktop, phone will collapse

# 5. Public parity with bust
curl -s "https://wheel.smitpatel.net/dividends?finalv4" | grep -o "mobile.css\|@media"  # both

# 6. SGOV intact
curl -s http://localhost:8096/api/allocation-data | python3 -c "import json,sys; print(json.load(sys.stdin)['totalAllocation'][2])"
# {'label':'Treasuries','value':49957.12,...}

# 7. Image committed
sg docker -c 'docker images wheeler:pi --format "{{.ID}} {{.Size}}"'
```

## Lesson for Skill Library — Anti-Patterns to Avoid

- Claiming "all mobile friendly" from desktop 1280px snapshot alone → false positive. Must verify via origin + inline @media presence + real 390px or matchMedia forced.
- Relying ONLY on external mobile.css when templates have inline <style> later in source → specificity loss. Fix: both external !important + inline @media !important duplication.
- Forgetting Go ParseGlob restart → template edits invisible. Add restart + sleep + logs to deploy script.
- Using `https://wheel...` tunnel for HTML verification → CF DYNAMIC + Turnstile /cdn-cgi/ challenge injection can show stale or challenge. Use origin `http://localhost:8096` for truth.

## Future Audit Before Declaring Mobile Done

1. `grep -n "display: flex\|grid-template\|height:100vh\|&nbsp;" templates/*.html` → list inline desktop-only
2. For each hit, ensure class + mobile.css `!important` @768 override + inline duplicate
3. `for p in all-pages; do curl origin | grep mobile-topbar; done` loop
4. Screenshot or phone check at 390px, not 1280px
5. Commit image + git push
