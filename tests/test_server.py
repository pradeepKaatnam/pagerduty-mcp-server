"""Tests for PagerDutyClient and the 4 MCP server tools."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp import Client, types

from client import PagerDutyAPIError, PagerDutyClient
from server import mcp

BASE = "https://api.pagerduty.com"


async def test_missing_api_token_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAGERDUTY_API_TOKEN", raising=False)
    client = PagerDutyClient()
    with pytest.raises(PagerDutyAPIError, match="PAGERDUTY_API_TOKEN"):
        await client.get("/services")


@respx.mock
async def test_retries_on_429_with_retry_after() -> None:
    route = respx.get(f"{BASE}/services").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": {"message": "Rate limit"}}),
            httpx.Response(200, json={"services": [{"id": "S1", "name": "Payments"}], "more": False}),
        ]
    )
    client = PagerDutyClient(api_token="test-token", backoff_base=0.01)
    items, has_more = await client.get_page("/services", "services")
    assert route.call_count == 2
    assert items == [{"id": "S1", "name": "Payments"}]
    assert has_more is False


@respx.mock
async def test_get_all_paginates_across_pages() -> None:
    def page_handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={"oncalls": [{"id": "1"}, {"id": "2"}], "more": True})
        return httpx.Response(200, json={"oncalls": [{"id": "3"}], "more": False})

    respx.get(f"{BASE}/oncalls").mock(side_effect=page_handler)
    client = PagerDutyClient(api_token="test-token")
    all_items = await client.get_all("/oncalls", "oncalls", page_size=2, max_items=10)
    assert [x["id"] for x in all_items] == ["1", "2", "3"]


@respx.mock
async def test_mcp_tools_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "test-token")

    respx.get(f"{BASE}/services").mock(
        return_value=httpx.Response(
            200,
            json={"services": [{"id": "PSVC_PAY", "name": "Payments", "status": "active"}], "more": False},
        )
    )
    respx.get(f"{BASE}/services/PSVC_PAY").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": {
                    "id": "PSVC_PAY",
                    "name": "Payments",
                    "escalation_policy": {"id": "PEP_PAY", "summary": "Payments Escalation"},
                }
            },
        )
    )
    oncalls_route = respx.get(f"{BASE}/oncalls").mock(
        return_value=httpx.Response(
            200,
            json={
                "oncalls": [
                    {
                        "escalation_level": 1,
                        "user": {"id": "PUSER1", "name": "Kyle Martinez"},
                        "schedule": {"summary": "Primary"},
                    },
                    {
                        "escalation_level": 2,
                        "user": {"id": "PUSER1", "name": "Kyle Martinez"},
                        "schedule": {"summary": "Secondary"},
                    },
                ],
                "more": False,
            },
        )
    )
    contacts_route = respx.get(f"{BASE}/users/PUSER1/contact_methods").mock(
        return_value=httpx.Response(
            200,
            json={
                "contact_methods": [
                    {"type": "email_contact_method", "label": "Work", "address": "kyle@example.com", "enabled": True},
                    {
                        "type": "phone_contact_method",
                        "label": "Mobile",
                        "country_code": 1,
                        "address": "4155550028",
                        "enabled": True,
                    },
                    {"type": "push_notification_contact_method", "address": "device-token"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/incidents").mock(
        return_value=httpx.Response(
            200,
            json={
                "incidents": [
                    {
                        "id": "Q1V5DUDCJXCNJT",
                        "incident_number": 6,
                        "title": "Checkout 500 spike",
                        "status": "triggered",
                        "urgency": "high",
                        "created_at": "2026-11-20T12:00:00Z",
                        "service": {"id": "PSVC_PAY", "summary": "Payments"},
                        "assignments": [{"assignee": {"summary": "Kyle Martinez"}}],
                        "acknowledgements": [],
                    }
                ],
                "more": False,
            },
        )
    )
    inc_route = respx.get(f"{BASE}/incidents/6").mock(
        return_value=httpx.Response(
            200,
            json={
                "incident": {
                    "id": "Q1V5DUDCJXCNJT",
                    "incident_number": 6,
                    "title": "Checkout 500 spike",
                    "status": "triggered",
                    "urgency": "high",
                    "priority": {"summary": "P1"},
                    "created_at": "2026-11-20T12:00:00Z",
                    "service": {"id": "PSVC_PAY", "summary": "Payments"},
                    "escalation_policy": {"id": "PEP_PAY", "summary": "Payments Escalation"},
                    "assignments": [{"assignee": {"summary": "Kyle Martinez"}}],
                    "acknowledgements": [],
                    "first_trigger_log_entry": {"channel": {"details": {"error_rate": "18%"}}},
                }
            },
        )
    )
    respx.get(f"{BASE}/incidents/Q1V5DUDCJXCNJT/alerts").mock(
        return_value=httpx.Response(
            200,
            json={
                "alerts": [
                    {
                        "id": "PALERT_1",
                        "status": "triggered",
                        "severity": "critical",
                        "summary": "HTTP 500 > 10%",
                        "body": {"details": {"host": "pay-01"}},
                    }
                ],
                "more": False,
            },
        )
    )
    respx.get(f"{BASE}/incidents/Q1V5DUDCJXCNJT/notes").mock(
        return_value=httpx.Response(
            200,
            json={"notes": [{"id": "PNOTE_1", "content": "Rolling back deploy", "user": {"summary": "Kyle"}}]},
        )
    )

    async with Client(mcp) as mcp_client:
        tools = await mcp_client.list_tools()
        assert {t.name for t in tools.tools} == {
            "list_services",
            "get_on_call_users",
            "list_open_incidents",
            "get_incident_context",
        }

        svcs = await mcp_client.call_tool("list_services", {"query": "Payments"})
        assert svcs.structured_content["services"][0]["id"] == "PSVC_PAY"

        oncall = await mcp_client.call_tool("get_on_call_users", {"service_id": "PSVC_PAY"})
        assert oncalls_route.calls.last.request.url.params.get_list("escalation_policy_ids[]") == ["PEP_PAY"]
        assert contacts_route.call_count == 1
        responder = oncall.structured_content["responders"][0]
        assert responder["first_name"] == "Kyle"
        assert responder["last_name"] == "Martinez"
        assert responder["escalation_levels"] == [1, 2]
        assert {c["type"] for c in responder["contact_methods"]} == {"email", "phone"}

        open_inc = await mcp_client.call_tool("list_open_incidents", {"urgency": "high"})
        assert open_inc.structured_content["count"] == 1
        assert open_inc.structured_content["incidents"][0]["acknowledged_by"] == []

        ctx = await mcp_client.call_tool("get_incident_context", {"incident_id": "6"})
        assert inc_route.calls.last.request.url.params.get_list("include[]") == ["first_trigger_log_entries"]
        assert ctx.structured_content["id"] == "Q1V5DUDCJXCNJT"
        assert ctx.structured_content["priority"] == "P1"
        assert ctx.structured_content["alerts"][0]["details"] == {"host": "pay-01"}
        assert ctx.structured_content["notes"][0]["content"] == "Rolling back deploy"


@respx.mock
async def test_api_error_surfaces_as_mcp_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "test-token")
    respx.get(f"{BASE}/services/MISSING").mock(
        return_value=httpx.Response(404, json={"error": {"message": "Not Found"}})
    )
    async with Client(mcp) as mcp_client:
        res = await mcp_client.call_tool("get_on_call_users", {"service_id": "MISSING"})
    assert res.is_error is True
    assert isinstance(res.content[0], types.TextContent)
    assert "HTTP 404" in res.content[0].text
