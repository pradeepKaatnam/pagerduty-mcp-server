# PagerDuty MCP Server

A Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server wrapping PagerDuty's REST API v2 for **on-call coverage and active incident triage**.

---

## Tools

| Tool | Purpose | Inputs | Output |
|---|---|---|---|
| `list_services` | List PagerDuty services to discover `service_id` | `query?`, `limit=25`, `offset=0` | `services` (`id`, `name`, `status`, `description`), `count`, `has_more` |
| `get_on_call_users` | Return who is currently on call for a service and how to contact them | `service_id` | Deduplicated `responders` (`first_name`, `last_name`, `escalation_levels`, `schedules`, `email` / `phone` / `sms` `contact_methods`) |
| `list_open_incidents` | List open (`triggered` and `acknowledged`) incidents | `service_id?`, `urgency?`, `limit=25`, `offset=0` | `incidents` (`id`, `incident_number`, `title`, `status`, `urgency`, `priority`, `acknowledged_by`, `assignees`, `service_id`, `service_name`, `created_at`), `count`, `has_more` |
| `get_incident_context` | Return investigation context for an incident | `incident_id` | Incident details, `acknowledged_by`, `assignees`, service, escalation policy, `teams`, initial `trigger_details`, `alerts`, and `notes` |

---

## Setup

### Prerequisites
- Python **3.10+**
- [`uv`](https://docs.astral.sh/uv/) or `pip`
- `PAGERDUTY_API_TOKEN` environment variable

### Install

With `uv`:
```bash
uv sync
```

Or with `pip`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp>=2,<3" "httpx>=0.27" pytest pytest-asyncio respx
```

---

## Running & Connecting to an LLM

### VS Code (`.vscode/mcp.json` included)
Open this folder in VS Code and start the `pagerduty` server from `.vscode/mcp.json` (it prompts for `PAGERDUTY_API_TOKEN` and launches `uv run server.py` over `stdio`).

### Claude Desktop / Cursor (`claude_desktop_config.json` / `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "pagerduty": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/pagerduty-mcp-server", "run", "server.py"],
      "env": {
        "PAGERDUTY_API_TOKEN": "<your_pagerduty_api_token>"
      }
    }
  }
}
```

### Claude Code CLI

```bash
claude mcp add pagerduty \
  -e PAGERDUTY_API_TOKEN=<your_pagerduty_api_token> \
  -- uv --directory /absolute/path/to/pagerduty-mcp-server run server.py
```

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
  -e PAGERDUTY_API_TOKEN=<your_pagerduty_api_token> \
  uv run server.py
```

---

## Running Tests

```bash
uv run pytest -v
```

---

## Project Structure

```text
├── client.py              # Generic async PagerDuty REST v2 client (auth, 429 retry, pagination)
├── server.py              # MCPServer("pagerduty") and the 4 @mcp.tool() functions
└── tests/
    └── test_server.py     # Client retry/pagination + end-to-end MCP tool tests
```

---

## Assumptions & Trade-offs

- **Service discovery via `list_services`:** PagerDuty endpoints filter by `service_id`, while users ask by service name. Exposing `list_services(query=...)` lets the LLM resolve names to IDs and ask the user for clarification when multiple services match a prefix.
- **Why `get_on_call_users` chains 3 endpoints:**
  1. `GET /oncalls` filters by `escalation_policy_ids[]` (it has no `service_ids[]` parameter), so the tool reads `GET /services/{service_id}` first to get `escalation_policy.id`.
  2. A responder on multiple escalation levels appears multiple times in `/oncalls`, so entries are deduplicated per user and PagerDuty's single display name is split into `first_name` / `last_name`.
  3. `/oncalls` only returns user references, so `/users/{id}/contact_methods` is fetched concurrently (`asyncio.gather`) for each unique responder to return email, phone, and SMS channels.
- **Open incidents window (`date_range="all"`):** PagerDuty's `GET /incidents` defaults to the last 30 days; passing `statuses[]=["triggered", "acknowledged"]` with `date_range="all"` ensures older unresolved incidents aren't missed.
- **Incident context (`get_incident_context`):** `GET /incidents/{id}` accepts either an incident number or an alphanumeric ID, whereas `GET /incidents/{id}/alerts` requires the alphanumeric ID. Fetching the incident first (`include[]=first_trigger_log_entries`) provides both initial trigger details and the canonical `incident["id"]`, then `/alerts` and `/notes` are fetched in parallel.
- **Pagination & rate limits:** `list_services` and `list_open_incidents` expose `limit` (`1..100`) and `offset` with `has_more`, while `/oncalls` auto-paginates (`get_all`, capped at 500) so multi-schedule policies aren't truncated. `PagerDutyClient` retries `429` (honoring `Retry-After`) and transient `5xx` / network errors with exponential backoff.
