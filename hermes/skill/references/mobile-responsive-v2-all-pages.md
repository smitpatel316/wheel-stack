# Mobile Responsive v2 — All Pages Fix 2026-08-02

User correction: "Monthly, options, dividends, metrics, symbols, admin, help, zen all are not mobile friendly still" after v1 only fixed dashboard + nav.

## Root Causes v2
- Monthly totals used `display:flex justify-content:space-between` + raw `&nbsp;×15` between totals → overflow 390px.
- Gains panel `flex:0 0 25%` / `flex:0 0 75%` no wrap, min 0 missing → side-by-side impossible phone.
- Monthly legend `flex:1` 10% with `max-height 700px flex-wrap` → overflow.
- Options `charts-wrapper` inline `style="display:flex;gap:20px"` + children `flex:3` / `flex:1` no min-width.
- Accordion 8-col grid `grid-template-columns: repeat(8,1fr)` — 8 cols on phone impossible.
- All-options `height:100vh !important` + `min-height:0 !important` + `overflow:auto !important` → broke main scroll, table invisible.
- Symbol stock-info inline `display:flex space-between width:100%` no wrap + summary items flex no min.
- Symbol charts inline `flex:1` ×4 → 4 cols phone.
- Dividends summary-grid was already responsive (auto-fit) but calendar `grid 2` and accordion-stats no wrap.
- Metrics `charts-grid 1fr 1fr` no breakpoint at 768.
- Help/admin/zen tab nav `display:flex` no wrap.

## Fixes Applied
### Template refactoring — extract inline to classes
- `monthly.html`:
  - Old totals div inline `background:#2d2d2d padding:15px flex justify-between` → `.monthly-totals-panel` class with `flex wrap gap12 justify space-between`.
  - Added `.month-picker-left`, `.monthly-totals-center` with `.total-group` spans (no nbsp).
  - Gains: old inline `display:flex gap20` → `.gains-panel` + `.gains-panel-pie min-width 240px` + `.gains-panel-bar min-width 300px`.
  - Charts+legend: old inline `display:flex gap20` + `flex:9` / `flex:1` → `.monthly-charts-with-legend` + `.monthly-charts-main flex 9 1 600px` + `.monthly-legend-side flex 1 1 120px`.
- `options.html`: `style="flex:3"` → `flex: 3 1 300px min-width:260px`, `style="flex:1"` → `flex:1 1 240px min-width:220px`.
- `all-options.html`: `height:100vh !important` → `min-height:60vh`, removed `min-height:0 !important` / `max-height:none !important` / `overflow:auto !important`, simplified `min-height:200px` / `80px`.
- `symbol.html`: `stock-info inline flex between` → `.stock-info` class + `.stock-metrics-row flex wrap`, charts inline `flex:1` → bare divs inside `.symbol-charts-row` (flex handles).
- All templates: added `<link mobile.css>` (scripted batch ensures all 13).

### mobile.css v2 — 14,026 bytes, 16× media queries
Added:
```
.monthly-totals-panel flex wrap + @768 column align-stretch, picker width 100%, center column align-start font 15px, hide balance spacer
.monthly-totals-center flex wrap → column phone
.gains-panel flex wrap → column phone, pie/bar 100% width min-width 0
.monthly-charts-with-legend flex wrap → column @768
.charts-grid @1100 1fr→1col already but extended @768 padding 12px chart-container 260px
.dividends-summary-grid 2→1 grid via inner .summary-grid 1fr 1fr phone → 1fr @420
.dividends-calendar 2→1 col @768 + @1100
.accordion-header column @768, stats gap10 wrap
.stock-info column @768, metrics row gap8
.symbol-charts-row > div 100% width @768 !important
.filter-section flex-wrap, inputs width 100%
.tab-navigation flex-wrap gap6, tab-btn 13px
.zen-header-row column center, zen-portfolio gap, zen-stock-prices grid 1fr1fr
.page-header flex column, financial-summary wrap
All overflow-x hidden containment html body max-width 100vw
```

## QA After v2
- `sg docker -c 'docker cp ...'` all templates + chmod a+r + restart + commit wheeler:pi e4ec83 104MB
- `curl localhost:8096/ | grep mobile-topbar` → mobile-topbar hit (desktop hidden via CSS, correct)
- `curl localhost:8096/static/css/mobile.css | wc -c` 14026 (was 9764 v1)
- Allocation API Treasuries $49,957 SGOV single row intact
- Cloudflare challenge injection `<a href="/cdn-cgi/...` present in HTML → CF bot protection, not bug — still serves 200, bypass with ?bust=.
- Browser snapshots: dividends, metrics, help, zen all show Toggle navigation + Close menu (hamburger) after cache bust

## Lesson
After first hamburger pass, user still sees breakage if deep pages keep inline flex without min-width/wrap. Fix pattern: extract every `style="flex:N"` to class with `flex N 1 min-width` + `flex-wrap`, replace `&nbsp;` spacing with `gap` + `flex-wrap`, kill `100vh !important` fullscreen locks that hide tables, convert 8-col grids to 2-col @768.

Include in skill: search_templates `grep -n "display: flex" templates/*.html` and replace with responsive classes before declaring done.
