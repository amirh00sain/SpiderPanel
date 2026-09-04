# SpiderPanel Regression Report

Final validation after transport, node, worker/reverse, Cloudflare, inbound and UI fixes.

- Python regression tests: 16 passed.
- Python syntax: main.py, xhttp_siz10.py, relay_vless.py compiled successfully.
- Frontend JavaScript: all inline script blocks in index.html and login.html passed `node --check`.
- Worker copies: worker/_worker.js and root _worker.js are byte-identical.
- XHTTP: stable per-user base path + X-Session/X-Seq metadata; legacy path compatibility retained.
- WS / XHTTP / gRPC: separate settings and independent link generation.
- Trojan: native Trojan-over-WebSocket handshake parsing on the shared WS relay; generated links use trojan://.
- Node inbound: checkbox selection is authoritative; checked nodes receive the user, unchecked nodes are deleted.
- Cloudflare token UI: no fake prefilled custom-permission URL; opens the official token page with explicit permission guidance.
