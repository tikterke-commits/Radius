"""Drop-in async client for the RADIUS engine's admin API, for Atlas.

Copy into the Atlas tree (e.g. backend/app/integrations/radius_engine.py).
Only dependency: httpx. Every method returns the engine's JSON as-is (dicts
and lists); raise_for_status semantics are wrapped in EngineError so Atlas
routes can turn engine 404/409/422 into the same status for the browser.

Units on the wire are bps and bytes. Convert to Mbps / GB at the UI edge.

    engine = RadiusEngine(
        base_url=settings.RADIUS_ENGINE_URL,          # https://10.140.140.20:8443
        api_key=settings.RADIUS_ENGINE_API_KEY,
        verify=settings.RADIUS_ENGINE_CA_BUNDLE,      # Caddy root.crt path
    )
    page = await engine.list_subscribers(q="ali", online=True, actor=user.username)

`actor` is recorded in the engine's audit log — always pass the Atlas user.
"""

from __future__ import annotations

from typing import Any

import httpx


class EngineError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"engine returned {status}: {detail}")
        self.status = status
        self.detail = detail


class RadiusEngine:
    def __init__(self, base_url: str, api_key: str, verify: bool | str = True, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-API-Key": api_key},
            verify=verify,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, actor: str, *, json: Any = None, params: dict | None = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._client.request(method, path, json=json, params=params, headers={"X-Actor": actor})
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise EngineError(resp.status_code, detail)
        return None if resp.status_code == 204 else resp.json()

    # --- dashboard / control ------------------------------------------------
    async def stats(self, actor: str):
        return await self._call("GET", "/stats", actor)

    async def actuation_status(self, actor: str):
        return await self._call("GET", "/actuation", actor)

    async def set_panic(self, on: bool, actor: str):
        return await self._call("POST", "/actuation/panic", actor, json={"on": on})

    # --- profiles -----------------------------------------------------------
    async def list_profiles(self, actor: str):
        return await self._call("GET", "/profiles", actor)

    async def get_profile(self, profile_id: str, actor: str):
        return await self._call("GET", f"/profiles/{profile_id}", actor)

    async def create_profile(self, body: dict, actor: str):
        return await self._call("POST", "/profiles", actor, json=body)

    async def update_profile(self, profile_id: str, changes: dict, actor: str):
        return await self._call("PATCH", f"/profiles/{profile_id}", actor, json=changes)

    async def delete_profile(self, profile_id: str, actor: str):
        return await self._call("DELETE", f"/profiles/{profile_id}", actor)

    async def simulate_profile(self, profile_id: str, actor: str, at: str | None = None,
                               today_bytes: int = 0, period_bytes: int = 0):
        return await self._call("POST", f"/profiles/{profile_id}/simulate", actor,
                                json={"at": at, "today_bytes": today_bytes, "period_bytes": period_bytes})

    # --- daily policies -----------------------------------------------------
    async def list_daily_policies(self, actor: str):
        return await self._call("GET", "/daily-policies", actor)

    async def get_daily_policy(self, policy_id: str, actor: str):
        return await self._call("GET", f"/daily-policies/{policy_id}", actor)

    async def create_daily_policy(self, body: dict, actor: str):
        return await self._call("POST", "/daily-policies", actor, json=body)

    async def update_daily_policy(self, policy_id: str, changes: dict, actor: str):
        return await self._call("PATCH", f"/daily-policies/{policy_id}", actor, json=changes)

    async def delete_daily_policy(self, policy_id: str, actor: str):
        return await self._call("DELETE", f"/daily-policies/{policy_id}", actor)

    # --- time-of-day policies -----------------------------------------------
    async def list_time_policies(self, actor: str):
        return await self._call("GET", "/time-policies", actor)

    async def get_time_policy(self, policy_id: str, actor: str):
        return await self._call("GET", f"/time-policies/{policy_id}", actor)

    async def create_time_policy(self, body: dict, actor: str):
        return await self._call("POST", "/time-policies", actor, json=body)

    async def update_time_policy(self, policy_id: str, changes: dict, actor: str):
        return await self._call("PATCH", f"/time-policies/{policy_id}", actor, json=changes)

    async def delete_time_policy(self, policy_id: str, actor: str):
        return await self._call("DELETE", f"/time-policies/{policy_id}", actor)

    # --- NAS ------------------------------------------------------------------
    async def list_nas(self, actor: str):
        return await self._call("GET", "/nas", actor)

    async def create_nas(self, body: dict, actor: str):
        return await self._call("POST", "/nas", actor, json=body)

    async def update_nas(self, nas_id: str, changes: dict, actor: str):
        return await self._call("PATCH", f"/nas/{nas_id}", actor, json=changes)

    async def delete_nas(self, nas_id: str, actor: str):
        return await self._call("DELETE", f"/nas/{nas_id}", actor)

    async def sync_nas_clients(self, actor: str):
        return await self._call("POST", "/nas/sync-clients", actor)

    # --- subscribers ----------------------------------------------------------
    async def list_subscribers(self, actor: str, q: str | None = None, status: str | None = None,
                               online: bool | None = None, profile_id: str | None = None,
                               expiring_within_days: int | None = None, page: int = 1,
                               page_size: int = 50, sort: str = "username"):
        return await self._call("GET", "/subscribers", actor, params={
            "q": q, "status": status, "online": None if online is None else str(online).lower(),
            "profile_id": profile_id, "expiring_within_days": expiring_within_days,
            "page": page, "page_size": page_size, "sort": sort})

    async def create_subscriber(self, body: dict, actor: str):
        return await self._call("POST", "/subscribers", actor, json=body)

    async def get_subscriber(self, subscriber_id: str, actor: str):
        return await self._call("GET", f"/subscribers/{subscriber_id}", actor)

    async def get_subscriber_by_username(self, username: str, actor: str):
        return await self._call("GET", f"/subscribers/by-username/{username}", actor)

    async def update_subscriber(self, subscriber_id: str, changes: dict, actor: str):
        return await self._call("PATCH", f"/subscribers/{subscriber_id}", actor, json=changes)

    async def delete_subscriber(self, subscriber_id: str, actor: str):
        return await self._call("DELETE", f"/subscribers/{subscriber_id}", actor)

    async def enable_subscriber(self, subscriber_id: str, actor: str):
        return await self._call("POST", f"/subscribers/{subscriber_id}/enable", actor)

    async def disable_subscriber(self, subscriber_id: str, actor: str):
        return await self._call("POST", f"/subscribers/{subscriber_id}/disable", actor)

    async def set_password(self, subscriber_id: str, actor: str, password: str | None = None, generate: bool = False):
        return await self._call("POST", f"/subscribers/{subscriber_id}/password", actor,
                                json={"password": password, "generate": generate})

    async def renew(self, subscriber_id: str, actor: str, days: int | None = None, reset_quota: bool | None = None):
        return await self._call("POST", f"/subscribers/{subscriber_id}/renew", actor,
                                json={"days": days, "reset_quota": reset_quota})

    async def change_profile(self, subscriber_id: str, profile_id: str, actor: str, when: str = "now",
                             at: str | None = None, reset_quota: bool = True):
        return await self._call("POST", f"/subscribers/{subscriber_id}/change-profile", actor,
                                json={"profile_id": profile_id, "when": when, "at": at, "reset_quota": reset_quota})

    async def cancel_scheduled_change(self, subscriber_id: str, actor: str):
        return await self._call("DELETE", f"/subscribers/{subscriber_id}/scheduled-change", actor)

    async def reset_quota(self, subscriber_id: str, actor: str, period: str = "all"):
        return await self._call("POST", f"/subscribers/{subscriber_id}/quota/reset", actor, json={"period": period})

    async def topup(self, subscriber_id: str, nbytes: int, actor: str, kind: str = "rxtx", period: str = "monthly"):
        return await self._call("POST", f"/subscribers/{subscriber_id}/quota/topup", actor,
                                json={"bytes": nbytes, "kind": kind, "period": period})

    async def mac(self, subscriber_id: str, action: str, actor: str, mac: str | None = None):
        return await self._call("POST", f"/subscribers/{subscriber_id}/mac", actor, json={"action": action, "mac": mac})

    async def disconnect(self, subscriber_id: str, actor: str):
        return await self._call("POST", f"/subscribers/{subscriber_id}/disconnect", actor)

    async def reconcile(self, subscriber_id: str, actor: str):
        return await self._call("POST", f"/subscribers/{subscriber_id}/reconcile", actor)

    async def usage(self, subscriber_id: str, actor: str, days: int = 30):
        return await self._call("GET", f"/subscribers/{subscriber_id}/usage", actor, params={"days": days})

    async def session_history(self, subscriber_id: str, actor: str, limit: int = 50):
        return await self._call("GET", f"/subscribers/{subscriber_id}/sessions", actor, params={"limit": limit})

    async def subscriber_auth_log(self, subscriber_id: str, actor: str, limit: int = 50):
        return await self._call("GET", f"/subscribers/{subscriber_id}/auth-log", actor, params={"limit": limit})

    async def bulk(self, ids: list[str], action: str, actor: str, profile_id: str | None = None, days: int | None = None):
        return await self._call("POST", "/subscribers/bulk", actor,
                                json={"ids": ids, "action": action, "profile_id": profile_id, "days": days})

    # --- sessions / logs ------------------------------------------------------
    async def online_sessions(self, actor: str, nas_ip: str | None = None, q: str | None = None, limit: int = 500):
        return await self._call("GET", "/sessions/online", actor, params={"nas_ip": nas_ip, "q": q, "limit": limit})

    async def disconnect_session(self, acct_session_id: str, actor: str):
        return await self._call("POST", f"/sessions/{acct_session_id}/disconnect", actor)

    async def auth_log(self, actor: str, result: str | None = None, username: str | None = None, limit: int = 100):
        return await self._call("GET", "/auth-log", actor, params={"result": result, "username": username, "limit": limit})

    async def audit(self, actor: str, subject: str | None = None, action: str | None = None,
                    by: str | None = None, limit: int = 100):
        return await self._call("GET", "/audit", actor,
                                params={"subject": subject, "action": action, "actor": by, "limit": limit})
