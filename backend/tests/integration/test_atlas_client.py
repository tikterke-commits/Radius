"""The drop-in Atlas client (clients/atlas/radius_engine.py) against the real
app — proves the client's paths and payloads match the server, so Atlas can't
be built against a contract that drifted."""

import importlib.util
from pathlib import Path

import httpx
import pytest

from tests.integration.test_admin_api import KEY, admin, engine_settings  # noqa: F401 fixtures

pytestmark = pytest.mark.asyncio

CLIENT_PATH = Path(__file__).resolve().parents[3] / "clients" / "atlas" / "radius_engine.py"


def load_client_module():
    spec = importlib.util.spec_from_file_location("radius_engine", CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_atlas_client_round_trip(admin):  # noqa: F811
    mod = load_client_module()
    from app.main import app

    engine = mod.RadiusEngine(base_url="http://test", api_key=KEY)
    engine._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1",
                                       headers={"X-API-Key": KEY})
    actor = "mhamad"

    dp = await engine.create_daily_policy({"name": "Std", "tiers": [
        {"after_bytes": 5_000_000_000, "rx_limit_bps": 1_000_000, "tx_limit_bps": 4_000_000}]}, actor)
    tp = await engine.create_time_policy({"name": "Peak", "windows": [
        {"start": "17:00", "end": "02:00", "speed_multiplier": 0.25}]}, actor)
    prof = await engine.create_profile({"name": "Standard", "rx_limit_bps": 2_000_000, "tx_limit_bps": 8_000_000,
                                        "daily_policy_id": dp["id"], "time_of_day_policy_id": tp["id"]}, actor)
    sim = await engine.simulate_profile(prof["id"], actor, at="2026-12-02T16:30:00+00:00")
    assert sim["reply_attributes"]["Mikrotik-Rate-Limit"] == "500000/2000000"  # 18:30 Beirut = peak

    created = await engine.create_subscriber({"username": "c1", "generate_password": True,
                                              "profile_id": prof["id"]}, actor)
    sid = created["subscriber"]["id"]
    assert (await engine.get_subscriber_by_username("c1", actor))["subscriber"]["id"] == sid
    await engine.update_subscriber(sid, {"notes": "via client"}, actor)
    await engine.renew(sid, actor, days=7)
    await engine.change_profile(sid, prof["id"], actor, when="next_renewal")
    await engine.cancel_scheduled_change(sid, actor)
    assert len(await engine.usage(sid, actor, days=7)) == 7
    page = await engine.list_subscribers(actor, q="c1", online=False)
    assert page["total"] == 1
    assert (await engine.stats(actor))["subscribers"]["total"] == 1
    assert (await engine.actuation_status(actor))["dry_run"] is False

    with pytest.raises(mod.EngineError) as err:
        await engine.delete_profile(prof["id"], actor)
    assert err.value.status == 409

    await engine.delete_subscriber(sid, actor)
    await engine._client.aclose()
