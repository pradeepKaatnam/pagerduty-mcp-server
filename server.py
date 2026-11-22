"""PagerDuty MCP Server for on-call coverage and active incident triage."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from client import PagerDutyAPIError, PagerDutyClient

mcp = MCPServer(
    "pagerduty",
    instructions=(
        "PagerDuty MCP server for on-call coverage and active incident triage. "
        "When given a service name, call `list_services(query=...)` first to find its `service_id`; "
        "if multiple services match, ask the user which service they mean before calling other tools. "
        "When a tool returns `has_more: true` and the user did not ask to fetch all pages, "
        "mention that more results are available and they can ask for the next page."
    ),
)

pd = PagerDutyClient()

_CONTACT_TYPES = {
    "email_contact_method": "email",
    "phone_contact_method": "phone",
    "sms_contact_method": "sms",
}


def _split_name(full_name: str) -> tuple[str, str]:
    cleaned = " ".join((full_name or "").split())
    if not cleaned:
        return "", ""
    parts = cleaned.split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def _format_contacts(raw_methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for cm in raw_methods:
        channel = _CONTACT_TYPES.get(cm.get("type", ""))
        address = cm.get("address")
        if not channel or not address:
            continue
        item: dict[str, Any] = {
            "type": channel,
            "label": cm.get("label") or cm.get("summary"),
            "address": str(address),
        }
        if cm.get("country_code") is not None:
            item["country_code"] = cm["country_code"]
        if isinstance(cm.get("enabled"), bool):
            item["enabled"] = cm["enabled"]
        contacts.append(item)
    return contacts


def _ref_name(obj: dict[str, Any] | None) -> str | None:
    return (obj.get("name") or obj.get("summary")) if obj else None


def _extract_names(entries: list[dict[str, Any]], nested_key: str) -> list[str]:
    names: list[str] = []
    for item in entries:
        name = _ref_name(item.get(nested_key))
        if name and name not in names:
            names.append(name)
    return names


@mcp.tool()
async def list_services(
    query: Annotated[
        str | None,
        Field(description="Optional case-insensitive substring to filter services by name."),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=100, description="Maximum number of services to return (1-100)."),
    ] = 25,
    offset: Annotated[
        int,
        Field(ge=0, description="Pagination offset (>= 0)."),
    ] = 0,
) -> dict[str, Any]:
    """List PagerDuty services. Call this first when given a service name to find its
    `service_id`; if multiple services match, ask the user to confirm the exact service.
    """
    params = {"query": query.strip()} if query and query.strip() else None
    try:
        raw_services, has_more = await pd.get_page(
            "/services",
            "services",
            params=params,
            limit=limit,
            offset=offset,
        )
    except PagerDutyAPIError as exc:
        raise ToolError(str(exc)) from exc

    services = [
        {
            "id": s["id"],
            "name": _ref_name(s) or "",
            "status": s.get("status"),
            "description": s.get("description") or None,
        }
        for s in raw_services
    ]
    return {"services": services, "count": len(services), "has_more": has_more}


@mcp.tool()
async def get_on_call_users(
    service_id: Annotated[
        str,
        Field(description="PagerDuty service ID. Call `list_services` first if you only have a service name."),
    ],
    limit: Annotated[
        int,
        Field(ge=1, le=100, description="Maximum number of on-call entries to inspect per page (1-100)."),
    ] = 25,
    offset: Annotated[
        int,
        Field(ge=0, description="Pagination offset (>= 0)."),
    ] = 0,
) -> dict[str, Any]:
    """Return the people currently on call for a PagerDuty service, including their first name,
    last name, escalation levels, schedules, and email/phone/SMS contact methods.
    """
    service_id = service_id.strip()
    if not service_id:
        raise ToolError("service_id cannot be empty.")

    try:
        service = (await pd.get(f"/services/{service_id}"))["service"]
        ep = service["escalation_policy"]

        oncalls, has_more = await pd.get_page(
            "/oncalls",
            "oncalls",
            params={"escalation_policy_ids[]": [ep["id"]]},
            limit=limit,
            offset=offset,
        )

        grouped: dict[str, dict[str, Any]] = {}
        for entry in oncalls:
            user = entry.get("user") or {}
            uid = user.get("id")
            if not uid:
                continue
            if uid not in grouped:
                grouped[uid] = {"user": user, "levels": set(), "schedules": []}
            if isinstance(entry.get("escalation_level"), int):
                grouped[uid]["levels"].add(entry["escalation_level"])
            sched_name = _ref_name(entry.get("schedule"))
            if sched_name and sched_name not in grouped[uid]["schedules"]:
                grouped[uid]["schedules"].append(sched_name)

        user_ids = list(grouped.keys())
        tasks = [pd.get(f"/users/{uid}/contact_methods") for uid in user_ids]
        contact_responses = await asyncio.gather(*tasks)
    except PagerDutyAPIError as exc:
        raise ToolError(str(exc)) from exc

    responders: list[dict[str, Any]] = []
    for i in range(len(user_ids)):
        uid = user_ids[i]
        data = grouped[uid]
        raw_methods = contact_responses[i].get("contact_methods") or []
        first_name, last_name = _split_name(_ref_name(data["user"]) or "")
        responders.append(
            {
                "user_id": uid,
                "first_name": first_name,
                "last_name": last_name,
                "escalation_levels": sorted(data["levels"]),
                "schedules": data["schedules"],
                "contact_methods": _format_contacts(raw_methods),
            }
        )

    responders.sort(key=lambda r: (r["escalation_levels"][0] if r["escalation_levels"] else 999, r["first_name"]))
    return {
        "service_id": service["id"],
        "service_name": _ref_name(service) or "",
        "escalation_policy_name": _ref_name(ep),
        "responders": responders,
        "has_more": has_more,
    }


@mcp.tool()
async def list_open_incidents(
    service_id: Annotated[
        str | None,
        Field(description="Optional PagerDuty service ID to filter incidents."),
    ] = None,
    urgency: Annotated[
        Literal["high", "low"] | None,
        Field(description="Optional urgency filter ('high' or 'low')."),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=100, description="Maximum number of incidents to return (1-100)."),
    ] = 25,
    offset: Annotated[
        int,
        Field(ge=0, description="Pagination offset (>= 0)."),
    ] = 0,
) -> dict[str, Any]:
    """List open PagerDuty incidents (`triggered` and `acknowledged`)."""
    params: dict[str, Any] = {
        "statuses[]": ["triggered", "acknowledged"],
        "date_range": "all",
        "sort_by": "created_at:desc",
    }
    if service_id and service_id.strip():
        params["service_ids[]"] = [service_id.strip()]
    if urgency:
        params["urgencies[]"] = [urgency]

    try:
        raw_incidents, has_more = await pd.get_page(
            "/incidents",
            "incidents",
            params=params,
            limit=limit,
            offset=offset,
        )
    except PagerDutyAPIError as exc:
        raise ToolError(str(exc)) from exc

    incidents = [
        {
            "id": inc["id"],
            "incident_number": inc.get("incident_number"),
            "title": inc.get("title") or inc.get("summary") or "",
            "status": inc["status"],
            "urgency": inc["urgency"],
            "priority": _ref_name(inc.get("priority")),
            "acknowledged_by": _extract_names(inc.get("acknowledgements") or [], "acknowledger"),
            "assignees": _extract_names(inc.get("assignments") or [], "assignee"),
            "service_id": (inc.get("service") or {}).get("id"),
            "service_name": _ref_name(inc.get("service")),
            "created_at": inc["created_at"],
        }
        for inc in raw_incidents
    ]
    return {"incidents": incidents, "count": len(incidents), "has_more": has_more}


@mcp.tool()
async def get_incident_context(
    incident_id: Annotated[
        str,
        Field(description="PagerDuty incident ID or incident number."),
    ],
) -> dict[str, Any]:
    """Return the context needed to investigate a PagerDuty incident: status, urgency,
    priority, acknowledgements, assignees, timestamps, service, escalation policy, teams,
    initial trigger details, alerts, and notes.
    """
    incident_id = incident_id.strip()
    if not incident_id:
        raise ToolError("incident_id cannot be empty.")

    try:
        inc_resp = await pd.get(
            f"/incidents/{incident_id}",
            params={"include[]": ["first_trigger_log_entries"]},
        )
        incident = inc_resp["incident"]
        canonical_id = incident["id"]

        alerts_page, notes_resp = await asyncio.gather(
            pd.get_page(f"/incidents/{canonical_id}/alerts", "alerts", limit=25, offset=0),
            pd.get(f"/incidents/{canonical_id}/notes"),
        )
        raw_alerts, alerts_has_more = alerts_page
    except PagerDutyAPIError as exc:
        raise ToolError(str(exc)) from exc

    service = incident.get("service") or {}
    channel = (incident.get("first_trigger_log_entry") or {}).get("channel") or {}

    alerts = [
        {
            "id": a["id"],
            "summary": a.get("summary"),
            "status": a.get("status"),
            "severity": a.get("severity"),
            "alert_key": a.get("alert_key"),
            "created_at": a.get("created_at"),
            "details": (a.get("body") or {}).get("details"),
        }
        for a in raw_alerts
    ]
    notes = [
        {
            "id": n["id"],
            "author": _ref_name(n.get("user")),
            "created_at": n.get("created_at"),
            "content": n.get("content") or "",
        }
        for n in (notes_resp.get("notes") or [])
    ]

    teams: list[str] = []
    for team in incident.get("teams") or []:
        team_name = _ref_name(team)
        if team_name:
            teams.append(team_name)

    return {
        "id": incident["id"],
        "incident_number": incident.get("incident_number"),
        "title": incident.get("title") or incident.get("summary") or "",
        "description": incident.get("description"),
        "status": incident["status"],
        "urgency": incident["urgency"],
        "priority": _ref_name(incident.get("priority")),
        "acknowledged_by": _extract_names(incident.get("acknowledgements") or [], "acknowledger"),
        "assignees": _extract_names(incident.get("assignments") or [], "assignee"),
        "created_at": incident["created_at"],
        "last_status_change_at": incident.get("last_status_change_at"),
        "resolved_at": incident.get("resolved_at"),
        "html_url": incident.get("html_url"),
        "service_id": service.get("id"),
        "service_name": _ref_name(service),
        "escalation_policy_name": _ref_name(incident.get("escalation_policy")),
        "teams": teams,
        "trigger_details": channel.get("details"),
        "alerts": alerts,
        "alerts_has_more": alerts_has_more,
        "notes": notes,
    }


if __name__ == "__main__":
    mcp.run()
