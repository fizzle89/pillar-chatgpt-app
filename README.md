# Pillar AI Visibility Checker (ChatGPT app / MCP server)

Remote MCP server (streamable HTTP at `/mcp`, stateless). Two read-only tools:
- `check_ai_visibility_readiness(url)`: one public page + robots.txt + llms.txt -> pass/fix checklist and AI crawler rules.
- `list_ai_crawlers()`: static reference.

Not done: no UI widget, no auth (none needed), no AI-answer querying or citation measurement. Public IPs only (SSRF guard), standard ports, 1 MB cap, 4 redirects.

Run: `pip install -r requirements.txt && python server.py` (PORT env). Render: build `pip install -r requirements.txt`, start `python server.py`.

## Launch layer (private MCP at /launch/mcp)
template -> instance -> launch -> job. Ask-first approval on every channel (MCP included), metered budget caps, independent rubric review (R1-R4), versioned methods with explicit upgrade, silent-failure flags, append-only hash-chained job events, cost per accepted job. Bearer tokens (env LAUNCH_OPERATOR_TOKEN, LAUNCH_OWNER_TOKEN, 24+ chars; unset = locked). In-memory state. Tests: `python -m unittest test_launch`.
