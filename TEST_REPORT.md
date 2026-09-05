# SpiderPanel Regression Test Report

Date: 2026-09-04

## Automated tests
- `pytest -q tests/test_inbounds.py tests/test_ui_static.py` — **12 passed**
- Inline JavaScript extraction + `node --check` — **PASS**

## Covered regressions
- Root `/` redirects to `/spider`.
- WireGuard keypair generation/persistence.
- Telegram external/internal fields persistence.
- TLS/Reality inbound creation.
- Invalid port validation returns HTTP 400.
- Custom Reality SNI and Target normalization/preservation.
- Invalid Reality Target paths rejected.
- Reality Xray config preserves custom `dest` and `serverNames`.
- SNI scanner sends the selected SNI as TLS `server_hostname` to the selected endpoint/IP.
- Fastest-SNI action can refresh from the source list when the saved-result file is empty.
- UI contains the SNI Scanner endpoint field and updated scanner actions.
