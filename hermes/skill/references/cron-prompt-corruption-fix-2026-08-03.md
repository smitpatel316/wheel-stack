# Cron Prompt Corruption — Shell Escaping Bug 2026-08-03

**Symptom:** Hermes cron job 014708b33a6a prompt contained `/usr/bin/bash.10`, `/usr/bin/bash.20`, `/usr/bin/bash.38` instead of `$0.10`, `$0.20`, `$0.38`

**Root cause:** Earlier session used `echo "$prompt" | ...` or sed replacement where `$0.10` interpreted by shell as `$0` = `/usr/bin/bash` + `.10` literal. When updating `~/.hermes/cron/jobs.json` via `cat << 'PROMPT'` with unescaped `$` inside double quotes, bash expanded `$0`, `$1` etc.

**Fix pattern:**
- Never use `echo "$var"` with `$` inside prompt that contains `$0.10` etc — always use single-quoted heredoc: `cat > /tmp/file << 'PROMPT'` (quoted delimiter prevents expansion)
- Better: use Python to rewrite JSON: `pathlib.Path.read_text() -> json.loads -> replace -> json.dumps -> write` — no shell expansion at all. Example used in final fix:

```python
import json, pathlib
p = pathlib.Path.home()/'.hermes/cron/jobs.json'
data = json.loads(p.read_text())
for job in data['jobs']:
    if job['id']=='014708b33a6a':
        job['prompt'] = """clean text with $0.10 literal, no escaping"""
p.write_text(json.dumps(data, indent=2))
```

- Validate after: `hermes cron list` + `jq '.prompt'` check no `/usr/bin/bash` artifacts, length ~9-10k chars expected for hybrid v2.2.

**Prevention:** All cron prompt updates must go via Python JSON rewrite, not bash heredoc with double quotes. Documented 2026-08-03 after rebuild to 9717 chars clean.

**Live verification:**
- Before: 7232 chars with `/usr/bin/bash.10` corruption
- After rebuild: 9717 chars clean, contains Closer, Roller, VIX, Spread sections, schedule `5 7,10,12 * * 1-5` PDT = ET 10:05/13:05/15:35
