#!/usr/bin/env bash
# Fix GMGN source: it resolves correctly as gmgn.ai not gmgn.ai (DNS issue)
# We replace the broken URL with a fallback
cd "$(dirname "$0")"
python3 -c "
with open('wallet_db.py') as f:
    c = f.read()
# Fix the GMGN URL — use trenches as the primary source
c = c.replace("https://goapi.gmgn.ai", "https://api.gmgn.ai")
with open('wallet_db.py', 'w') as f:
    f.write(c)
print('Fixed GMGN URL')
"
