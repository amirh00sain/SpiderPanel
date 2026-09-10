SpiderPanel Remote Worker Upgrade

- Worker creation no longer relies on a fixed spider-proxy name.
- Each new deployment receives an isolated worker name.
- KV namespace naming is isolated per worker.
- Existing public subscription renderer remains integrated through pages.py.
- Cloudflare permissions required:
  Workers Scripts, Workers KV Storage, Account access.
