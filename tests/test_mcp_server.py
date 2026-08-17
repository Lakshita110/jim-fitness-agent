"""The Garmin MCP server's auth layer — the part that matters most, since
this is the one server two different people (the athlete and their mom)
both call with their own bearer token. No real HTTP socket or Postgres
needed: an ASGI transport drives the mounted app in-process, same wiring
Vercel would use, and `jim.db` is monkeypatched exactly like the rest of the
test suite does for the JSON API.

All assertions live in one test function sharing one `mcp_app.lifespan`
context: pytest-asyncio's per-test fixture teardown resumes an async
generator fixture in a different asyncio Task than it was set up in, which
anyio's task-group cancel scope (opened by that lifespan) explicitly forbids
— a known framework interaction, not something in our code to work around
per-test."""

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

import jim.app as app_mod
import jim.mcp_server as mcp_server_mod
from jim import auth, db
from jim.app import mcp_app


def _asgi_client(
    headers: dict[str, str] | None = None, url: str = "http://testserver/mcp/"
) -> Client:
    def factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://testserver",
            **kwargs,
        )

    transport = StreamableHttpTransport(url, headers=headers, httpx_client_factory=factory)
    return Client(transport)


async def test_mcp_auth_and_multi_user_isolation(monkeypatch):
    monkeypatch.setattr(db, "ensure_migrated", lambda: None)

    async with mcp_app.lifespan(mcp_app):
        # No token at all.
        async with _asgi_client() as c:
            with pytest.raises(ToolError, match="missing token"):
                await c.call_tool("get_constraints", {})

        # Header present but not a bearer token, and no query-param fallback either.
        async with _asgi_client({"Authorization": "not-bearer x"}) as c:
            with pytest.raises(ToolError, match="missing token"):
                await c.call_tool("get_constraints", {})

        # Bearer scheme but a token that doesn't verify.
        async with _asgi_client({"Authorization": "Bearer garbage"}) as c:
            with pytest.raises(ToolError, match="invalid or expired"):
                await c.call_tool("get_constraints", {})

        # ?token= with an invalid value is rejected the same way.
        async with _asgi_client(url="http://testserver/mcp/?token=garbage") as c:
            with pytest.raises(ToolError, match="invalid or expired"):
                await c.call_tool("get_constraints", {})

        # A valid token resolves to the matching user_id, via header...
        monkeypatch.setattr(db, "get_constraints", lambda uid: f"constraints for {uid}")
        token = auth.create_session_token(101)
        async with _asgi_client({"Authorization": f"Bearer {token}"}) as c:
            result = await c.call_tool("get_constraints", {})
            assert result.data == "constraints for 101"

        # ...or, for clients that can't set headers (claude.ai's connector
        # dialog), via a ?token= query param on the connector URL instead.
        async with _asgi_client(url=f"http://testserver/mcp/?token={token}") as c:
            result = await c.call_tool("get_constraints", {})
            assert result.data == "constraints for 101"

        # The isolation check this whole design depends on: two different
        # tokens must never resolve to each other's data.
        store = {101: "athlete's knee/ankle limits", 202: "mom's shoulder limits"}
        monkeypatch.setattr(db, "get_constraints", lambda uid: store[uid])
        token_a = auth.create_session_token(101)
        token_b = auth.create_session_token(202)
        async with _asgi_client({"Authorization": f"Bearer {token_a}"}) as c:
            result_a = await c.call_tool("get_constraints", {})
        async with _asgi_client({"Authorization": f"Bearer {token_b}"}) as c:
            result_b = await c.call_tool("get_constraints", {})
        assert result_a.data == "athlete's knee/ankle limits"
        assert result_b.data == "mom's shoulder limits"

        # A write lands under the calling user's own id.
        writes: list[tuple[int, str]] = []
        monkeypatch.setattr(
            db, "set_constraints", lambda uid, content: writes.append((uid, content))
        )
        token_c = auth.create_session_token(303)
        async with _asgi_client({"Authorization": f"Bearer {token_c}"}) as c:
            write_result = await c.call_tool("set_constraints", {"content": "no jump squats"})
        assert write_result.data == {"ok": True}
        assert writes == [(303, "no jump squats")]


def test_normalize_kind_passes_through_the_real_values():
    for kind in (
        "strength", "conditioning", "mobility", "rest",
        "running", "cycling", "swimming", "walking", "hiking", "yoga",
        "pilates", "hiit", "rucking", "other",
    ):
        assert mcp_server_mod._normalize_kind(kind) == kind


def test_normalize_kind_maps_garmin_and_common_vocabulary():
    """The bug that actually happened: Claude read "strength_training" off
    get_scheduled_workouts/list_saved_workouts (Garmin's own sportType key)
    and passed it straight through, which used to blow up as a raw pydantic
    422 with no indication of what to try instead. A related bug: a plain
    walk used to get bucketed into "conditioning" (and from there silently
    tagged strength_training on Garmin — see test_garmin_payload.py) instead
    of being recognized as its own kind."""
    assert mcp_server_mod._normalize_kind("strength_training") == "strength"
    assert mcp_server_mod._normalize_kind("fitness_equipment") == "strength"
    assert mcp_server_mod._normalize_kind("cardio") == "conditioning"
    assert mcp_server_mod._normalize_kind("run") == "running"
    assert mcp_server_mod._normalize_kind("bike") == "cycling"
    assert mcp_server_mod._normalize_kind("swim") == "swimming"
    assert mcp_server_mod._normalize_kind("walk") == "walking"
    assert mcp_server_mod._normalize_kind("hike") == "hiking"
    assert mcp_server_mod._normalize_kind("ruck") == "rucking"
    assert mcp_server_mod._normalize_kind("stretching") == "mobility"
    # Case/whitespace tolerant, since a model won't always match exactly.
    assert mcp_server_mod._normalize_kind("  Cardio ") == "conditioning"


def test_normalize_kind_rejects_unknown_values_with_a_clear_message():
    with pytest.raises(ToolError, match="unrecognized kind"):
        mcp_server_mod._normalize_kind("interpretive dance")


def test_ensure_history_resyncs_today_every_call(monkeypatch):
    """The nightly cron runs once, early evening — before that night's
    sleep/HRV/body-battery data even exists. Without a re-sync on read,
    today's stored row stays stale (nulls) for the rest of the day. A live
    read is the moment worth paying one extra Garmin call for."""
    calls = []
    from jim.jobs import nightly as nightly_mod

    monkeypatch.setattr(nightly_mod, "_today_for_user", lambda uid: "2026-08-17")
    monkeypatch.setattr(nightly_mod, "sync_today", lambda uid: calls.append(("sync_today", uid)))

    from jim.tools import garmin as garmin_mod

    monkeypatch.setattr(
        garmin_mod, "backfill_if_empty",
        lambda uid, today, days=90: calls.append(("backfill_if_empty", uid)),
    )

    mcp_server_mod._ensure_history(42)

    assert ("backfill_if_empty", 42) in calls
    assert ("sync_today", 42) in calls


def test_ensure_history_swallows_failures(monkeypatch):
    """A Garmin hiccup during the opportunistic re-sync must not block the
    read the athlete actually asked for."""
    from jim.jobs import nightly as nightly_mod

    monkeypatch.setattr(nightly_mod, "_today_for_user", lambda uid: "2026-08-17")

    def boom(uid):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(nightly_mod, "sync_today", boom)

    mcp_server_mod._ensure_history(42)  # must not raise
