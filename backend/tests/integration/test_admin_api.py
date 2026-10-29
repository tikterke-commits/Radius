"""End-to-end through the real FastAPI app, real Postgres, real Redis and a
fake NAS speaking real RADIUS CoA over UDP: Atlas-style admin calls ->
rlm_rest calls -> worker ticks -> CoA/Disconnect on the wire.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import worker
from app.actuation.coa import PyradCoASender
from app.config import settings
from app.models.audit import AuditLog
from app.models.session import RadiusSession
from app.models.usage import UsageDaily
from app.policy.clock import to_local
from app.redis_client import get_redis
from tests.integration.fake_nas import FakeNas

pytestmark = pytest.mark.asyncio
GB = 1_000_000_000
KEY = "test-admin-key"


@pytest.fixture
def engine_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_api_key", KEY)
    monkeypatch.setattr(settings, "nas_secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "radius_clients_path", str(tmp_path / "clients" / "nas.conf"))
    monkeypatch.setattr(settings, "actuation_dry_run", False)
    monkeypatch.setattr(settings, "session_stale_after_s", 86400)
    monkeypatch.setattr(settings, "coa_timeout_s", 1.0)
    return tmp_path


@pytest_asyncio.fixture
async def admin(client, engine_settings):
    from app.api.admin import deps
    from app.main import app

    sender = PyradCoASender(timeout_s=1.0, retries=0)
    app.dependency_overrides[deps.get_sender] = lambda: sender
    client.headers.update({"X-API-Key": KEY, "X-Actor": "mhamad"})
    yield client


@pytest.fixture
def fake_nas():
    with FakeNas() as nas:
        yield nas


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def make_profile(admin, **overrides):
    body = {"name": "Premium 20M", "rx_limit_bps": 5_000_000, "tx_limit_bps": 20_000_000,
            "ip_pool": "pool-res", "interim_interval_s": 300, **overrides}
    r = await admin.post("/api/v1/profiles", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def make_subscriber(admin, profile_id, username="ali", **overrides):
    body = {"username": username, "password": "s3cret-pass", "profile_id": profile_id,
            "firstname": "Ali", "phone": "+96170000000", **overrides}
    r = await admin.post("/api/v1/subscribers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def register_nas(admin, nas: FakeNas, **overrides):
    r = await admin.post("/api/v1/nas", json={"name": "bng-fr-1", "ip": "127.0.0.1", "secret": nas.secret.decode(),
                                              "ros_version": "7.18.2", "coa_port": nas.port, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


async def login(admin, username="ali", mac="AA:BB:CC:00:00:01", session="sess-1"):
    r = await admin.post("/radius/authorize", json={"username": username, "nas_ip_address": "127.0.0.1",
                                                    "calling_station_id": mac})
    if r.status_code == 200:
        await admin.post("/radius/accounting", json={
            "username": username, "acct_status_type": "Start", "acct_session_id": session,
            "nas_ip_address": "127.0.0.1", "framed_ip_address": "100.64.0.10", "calling_station_id": mac})
    return r


async def interim(admin, rx, tx, username="ali", session="sess-1", rx_gw=0, tx_gw=0, t=300):
    r = await admin.post("/radius/accounting", json={
        "username": username, "acct_status_type": "Interim-Update", "acct_session_id": session,
        "nas_ip_address": "127.0.0.1", "acct_session_time": t, "acct_input_octets": rx,
        "acct_output_octets": tx, "acct_input_gigawords": rx_gw, "acct_output_gigawords": tx_gw})
    assert r.status_code == 204


# --- auth & basics -------------------------------------------------------------

async def test_admin_api_requires_key(client, engine_settings):
    assert (await client.get("/api/v1/profiles")).status_code == 401
    assert (await client.get("/api/v1/profiles", headers={"X-API-Key": "nope"})).status_code == 401


async def test_admin_api_disabled_without_key(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    assert (await client.get("/api/v1/profiles", headers={"X-API-Key": "x"})).status_code == 503


async def test_profile_crud_and_preview(admin):
    p = await make_profile(admin, burst_rx_bps=8_000_000, burst_tx_bps=30_000_000, priority=4)
    assert p["rate_limit_preview"].startswith("5000000/20000000 8000000/30000000")
    assert p["profile_name_normalized"] == "premium-20m"

    r = await admin.patch(f"/api/v1/profiles/{p['id']}", json={"tx_limit_bps": 25_000_000})
    assert r.json()["tx_limit_bps"] == 25_000_000 and r.json()["rx_limit_bps"] == 5_000_000

    bad = await admin.patch(f"/api/v1/profiles/{p['id']}", json={"custom_attributes": [
        {"attribute": "Mikrotik-Rate-Limit", "value": "1M/1M"}]})
    assert bad.status_code == 422

    await make_subscriber(admin, p["id"])
    assert (await admin.delete(f"/api/v1/profiles/{p['id']}")).status_code == 409


async def test_create_subscriber_then_radius_login(admin):
    p = await make_profile(admin)
    created = await admin.post("/api/v1/subscribers", json={
        "username": "zeina", "generate_password": True, "profile_id": p["id"], "static_ip": "100.64.9.9"})
    assert created.status_code == 201
    password = created.json()["generated_password"]
    assert created.json()["subscription"]["days_left"] in (29, 30)

    assert (await admin.post("/radius/authenticate", json={"username": "zeina", "password": password})).status_code == 200
    r = await admin.post("/radius/authorize", json={"username": "zeina", "nas_ip_address": "127.0.0.1"})
    assert r.status_code == 200
    assert r.json() == {"reply:Mikrotik-Rate-Limit": "5000000/20000000",
                        "reply:Framed-IP-Address": "100.64.9.9", "reply:Acct-Interim-Interval": 300}


async def test_duplicate_username_rejected(admin):
    p = await make_profile(admin)
    await make_subscriber(admin, p["id"])
    r = await admin.post("/api/v1/subscribers", json={"username": "ali", "password": "xxxxxx", "profile_id": p["id"]})
    assert r.status_code == 409


async def test_post_auth_writes_auth_log(admin, db_session):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await admin.post(f"/api/v1/subscribers/{sub['subscriber']['id']}/disable")
    r = await admin.post("/radius/authorize", json={"username": "ali", "nas_ip_address": "127.0.0.1"})
    assert r.status_code == 401
    await admin.post("/radius/post-auth", json={"username": "ali", "packet_type": "Access-Reject",
                                                "nas_ip_address": "127.0.0.1",
                                                "reply_message": r.json()["reply:Reply-Message"]})
    log = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}/auth-log")).json()
    assert log[0]["result"] == "reject" and log[0]["reason"] == "gate:status"


# --- accounting / usage ----------------------------------------------------------

async def test_gigawords_counted_past_4gib(admin, db_session):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await login(admin)
    await interim(admin, rx=100, tx=500)
    await interim(admin, rx=200, tx=700, tx_gw=1)  # download crossed 4 GiB: 1<<32 + 700

    usage = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}/usage?days=1")).json()
    assert usage[-1]["tx_bytes"] == (1 << 32) + 700
    assert usage[-1]["rx_bytes"] == 200


async def test_free_window_traffic_is_recorded_not_counted(admin, db_session):
    local = to_local(datetime.now(timezone.utc))
    start = (local - timedelta(hours=1)).strftime("%H:%M")
    end = (local + timedelta(hours=1)).strftime("%H:%M")
    dp = (await admin.post("/api/v1/daily-policies", json={
        "name": "Night free", "free_windows": [{"start": start, "end": end}]})).json()
    p = await make_profile(admin, daily_policy_id=dp["id"], quota_rxtx_monthly=10 * GB)
    sub = await make_subscriber(admin, p["id"])
    await login(admin)
    await interim(admin, rx=1_000, tx=9_000)

    detail = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}")).json()
    assert detail["usage"]["today_counted_tx"] == 0
    assert detail["quotas"][0]["remaining"] == 10 * GB
    usage = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}/usage?days=1")).json()
    assert usage[-1]["tx_bytes"] == 9_000 and usage[-1]["counted_tx_bytes"] == 0


async def test_concurrent_session_from_other_mac_rejected_same_mac_allowed(admin):
    p = await make_profile(admin)
    await make_subscriber(admin, p["id"])
    assert (await login(admin, mac="AA:BB:CC:00:00:01")).status_code == 200
    second = await admin.post("/radius/authorize", json={"username": "ali", "nas_ip_address": "127.0.0.1",
                                                         "calling_station_id": "AA:BB:CC:00:00:99"})
    assert second.status_code == 401 and second.json()["reply:Reply-Message"] == "concurrent_limit"
    redial = await admin.post("/radius/authorize", json={"username": "ali", "nas_ip_address": "127.0.0.1",
                                                         "calling_station_id": "aa:bb:cc:00:00:01"})
    assert redial.status_code == 200


# --- actuation ---------------------------------------------------------------------

async def test_daily_fup_crossing_pushes_coa_over_the_wire(admin, fake_nas, session_factory, db_session):
    dp = (await admin.post("/api/v1/daily-policies", json={
        "name": "Standard daily", "tiers": [{"after_bytes": 5 * GB, "rx_limit_bps": 1_000_000,
                                             "tx_limit_bps": 4_000_000}]})).json()
    p = await make_profile(admin, daily_policy_id=dp["id"])
    await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)
    await interim(admin, rx=0, tx=6 * GB % (1 << 32), tx_gw=(6 * GB) >> 32)

    summary = await worker.drain_reconcile_queue(session_factory, get_redis(), PyradCoASender(1.0, 0),
                                                 datetime.now(timezone.utc))
    assert summary["coa"] == 1
    pkt = fake_nas.received[-1]
    assert pkt["code"] == "coa" and pkt["authenticator_ok"]
    assert pkt["attributes"]["Mikrotik-Rate-Limit"] == "1000000/4000000"
    assert pkt["attributes"]["User-Name"] == "ali" and pkt["attributes"]["Acct-Session-Id"] == "sess-1"

    db_session.expire_all()
    s = await db_session.get(RadiusSession, "sess-1")
    assert s.last_reply["Mikrotik-Rate-Limit"] == "1000000/4000000"

    # Second pass: already in sync -> no packet.
    before = len(fake_nas.received)
    await admin.post("/api/v1/subscribers/" + str(s.subscriber_id) + "/reconcile")
    assert len(fake_nas.received) == before


async def test_disable_sends_disconnect(admin, fake_nas):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)
    r = await admin.post(f"/api/v1/subscribers/{sub['subscriber']['id']}/disable")
    assert r.json()["actuation"][0]["action"] == "disconnect"
    assert fake_nas.received[-1]["code"] == "disconnect"
    assert fake_nas.received[-1]["attributes"]["Framed-IP-Address"] == "100.64.0.10"


async def test_coa_nak_falls_back_to_disconnect(admin, session_factory):
    with FakeNas(nak=True) as nas:
        p = await make_profile(admin)
        sub = await make_subscriber(admin, p["id"])
        await register_nas(admin, nas)
        await login(admin)
        await admin.patch(f"/api/v1/subscribers/{sub['subscriber']['id']}", json={"tx_limit_override_bps": 1_000_000})
        await worker.drain_reconcile_queue(session_factory, get_redis(), PyradCoASender(1.0, 0),
                                           datetime.now(timezone.utc))
        assert [p["code"] for p in nas.received] == ["coa", "disconnect"]


async def test_dry_run_audits_but_sends_nothing(admin, fake_nas, monkeypatch, db_session):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)
    monkeypatch.setattr(settings, "actuation_dry_run", True)
    r = await admin.post(f"/api/v1/subscribers/{sub['subscriber']['id']}/disconnect")
    assert r.json()["results"][0]["detail"] == "dry-run: would disconnect"
    assert fake_nas.received == []
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "actuation.disconnect"))).scalars().all()
    assert rows and rows[0].nas_response == {"dry_run": True}


async def test_panic_switch_blocks_actuation(admin, fake_nas):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)
    assert (await admin.post("/api/v1/actuation/panic", json={"on": True})).json()["panic"] is True
    r = await admin.post(f"/api/v1/subscribers/{sub['subscriber']['id']}/disconnect")
    assert r.json()["results"][0]["detail"] == "panic switch on"
    assert fake_nas.received == []


async def test_peak_window_edge_detected_by_worker(admin, fake_nas, session_factory):
    now = datetime.now(timezone.utc)
    local = to_local(now)
    # Window starts 10 minutes from now and runs for an hour.
    start = (local + timedelta(minutes=10)).strftime("%H:%M")
    end = (local + timedelta(minutes=70)).strftime("%H:%M")
    tp = (await admin.post("/api/v1/time-policies", json={"name": "Peak", "windows": [
        {"start": start, "end": end, "applies_to": "all", "speed_multiplier": 0.25}]})).json()
    p = await make_profile(admin, time_of_day_policy_id=tp["id"])
    await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)

    redis = get_redis()
    async with session_factory() as db:
        assert await worker.detect_window_edges(db, redis, now) == 0  # records baseline
    async with session_factory() as db:
        assert await worker.detect_window_edges(db, redis, now + timedelta(minutes=15)) == 1
    await worker.drain_reconcile_queue(session_factory, redis, PyradCoASender(1.0, 0), now + timedelta(minutes=15))
    assert fake_nas.received[-1]["attributes"]["Mikrotik-Rate-Limit"] == "1250000/5000000"


async def test_expired_walled_garden_via_worker(admin, fake_nas, session_factory, db_session):
    p = await make_profile(admin, on_expired="walled_garden")
    sub = await make_subscriber(admin, p["id"])
    await register_nas(admin, fake_nas)
    await login(admin)
    from app.models.plan import Subscription
    import uuid as _uuid
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        s = await db.get(Subscription, _uuid.UUID(sub["subscriber"]["id"]))
        s.expiration = now - timedelta(minutes=1)
        await db.commit()
    async with session_factory() as db:
        assert await worker.sweep_expired(db, get_redis(), now) == 1
    await worker.drain_reconcile_queue(session_factory, get_redis(), PyradCoASender(1.0, 0), now)
    # Address list and rate both change; Address-List isn't CoA-mutable by
    # default, so the engine redials the session instead of half-applying.
    assert fake_nas.received[-1]["code"] == "disconnect"
    detail = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}")).json()
    assert detail["subscriber"]["status"] == "expired"
    assert detail["decision"]["reason"] == "ok:walled_garden:expiration"
    assert detail["decision"]["reply_attributes"]["Mikrotik-Address-List"] == "walled-garden"


# --- lifecycle -----------------------------------------------------------------------

async def test_renew_extends_from_later_of_now_and_expiry_and_resets_quota(admin, db_session):
    p = await make_profile(admin, quota_rxtx_monthly=50 * GB)
    sub = await make_subscriber(admin, p["id"])
    sid = sub["subscriber"]["id"]
    exp_before = datetime.fromisoformat(sub["subscription"]["expiration"])
    await login(admin)
    await interim(admin, rx=GB, tx=GB)
    r = (await admin.post(f"/api/v1/subscribers/{sid}/renew", json={})).json()
    assert datetime.fromisoformat(r["subscription"]["expiration"]) == exp_before + timedelta(days=30)
    assert r["quotas"][0]["remaining"] == 50 * GB


async def test_topup_and_quota_reset(admin):
    p = await make_profile(admin, quota_rxtx_monthly=10 * GB)
    sub = await make_subscriber(admin, p["id"])
    sid = sub["subscriber"]["id"]
    r = (await admin.post(f"/api/v1/subscribers/{sid}/quota/topup", json={"bytes": 5 * GB})).json()
    assert r["quotas"][0]["remaining"] == 15 * GB
    r = (await admin.post(f"/api/v1/subscribers/{sid}/quota/reset", json={"period": "monthly"})).json()
    assert r["quotas"][0]["remaining"] == 10 * GB
    nope = await admin.post(f"/api/v1/subscribers/{sid}/quota/topup", json={"bytes": 1, "period": "daily"})
    assert nope.status_code == 409


async def test_scheduled_profile_change_applied_by_worker(admin, session_factory):
    p1 = await make_profile(admin, name="Basic")
    p2 = await make_profile(admin, name="Elite", tx_limit_bps=50_000_000)
    sub = await make_subscriber(admin, p1["id"])
    sid = sub["subscriber"]["id"]
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    r = (await admin.post(f"/api/v1/subscribers/{sid}/change-profile",
                          json={"profile_id": p2["id"], "when": "at", "at": at.isoformat()})).json()
    assert r["subscription"]["next_profile_change"]["profile_name"] == "Elite"
    async with session_factory() as db:
        assert await worker.apply_scheduled_changes(db, get_redis(), at + timedelta(seconds=1)) == 1
        await db.commit()
    detail = (await admin.get(f"/api/v1/subscribers/{sid}")).json()
    assert detail["subscription"]["profile_name"] == "Elite"
    assert detail["subscription"]["next_profile_change"] is None


async def test_mac_bind_current_then_mismatch_rejects(admin):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    sid = sub["subscriber"]["id"]
    await login(admin, mac="aa-bb-cc-00-00-07")
    r = (await admin.post(f"/api/v1/subscribers/{sid}/mac", json={"action": "bind_current"})).json()
    assert r["subscriber"]["mac_binding"] == "AA:BB:CC:00:00:07"
    other = await admin.post("/radius/authorize", json={"username": "ali", "nas_ip_address": "127.0.0.1",
                                                        "calling_station_id": "AA:BB:CC:00:00:08"})
    assert other.json()["reply:Reply-Message"] == "mac_binding_mismatch"


async def test_list_filters_and_stats(admin):
    p = await make_profile(admin)
    await make_subscriber(admin, p["id"], username="online1")
    await make_subscriber(admin, p["id"], username="offline1")
    old = await make_subscriber(admin, p["id"], username="expired1",
                                expiration=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    await login(admin, username="online1")

    online = (await admin.get("/api/v1/subscribers?online=true")).json()
    assert [i["username"] for i in online["items"]] == ["online1"]
    expired = (await admin.get("/api/v1/subscribers?status=expired")).json()
    assert [i["id"] for i in expired["items"]] == [old["subscriber"]["id"]]
    search = (await admin.get("/api/v1/subscribers?q=line1")).json()
    assert search["total"] == 2

    stats = (await admin.get("/api/v1/stats")).json()
    assert stats["subscribers"]["total"] == 3 and stats["subscribers"]["online"] == 1
    assert stats["subscribers"]["expired"] == 1


async def test_nas_create_renders_clients_file_without_leaking_secret(admin, engine_settings):
    r = await admin.post("/api/v1/nas", json={"name": "bng 1", "ip": "10.140.20.1", "secret": "Sup3r-secret!",
                                              "ros_version": "6.49.19"})
    body = r.json()
    assert r.status_code == 201 and body["clients_file"]["written"] and body["clients_file"]["clients"] == 1
    assert "secret" not in body["nas"] and "Sup3r-secret!" not in r.text
    conf = (engine_settings / "clients" / "nas.conf").read_text()
    assert "ipaddr = 10.140.20.1" in conf and 'secret = "Sup3r-secret!"' in conf and "client nas_bng_1" in conf
    assert (await admin.post("/api/v1/nas", json={"name": "dup", "ip": "10.140.20.1", "secret": "xxxxxxxx",
                                                  "ros_version": "7"})).status_code == 409


async def test_delete_subscriber_removes_everything(admin, db_session):
    p = await make_profile(admin, quota_rxtx_monthly=GB)
    sub = await make_subscriber(admin, p["id"])
    sid = sub["subscriber"]["id"]
    await login(admin)
    await interim(admin, rx=10, tx=10)
    assert (await admin.delete(f"/api/v1/subscribers/{sid}")).status_code == 204
    assert (await admin.get(f"/api/v1/subscribers/{sid}")).status_code == 404
    db_session.expire_all()
    assert (await db_session.execute(select(UsageDaily))).first() is None


async def test_simulate_shows_peak_and_fup(admin):
    dp = (await admin.post("/api/v1/daily-policies", json={
        "name": "D", "tiers": [{"after_bytes": GB, "rx_limit_bps": 100, "tx_limit_bps": 200}]})).json()
    p = await make_profile(admin, daily_policy_id=dp["id"])
    r = (await admin.post(f"/api/v1/profiles/{p['id']}/simulate", json={"today_bytes": 2 * GB})).json()
    assert r["reply_attributes"]["Mikrotik-Rate-Limit"] == "100/200"
    assert "daily FUP tier" in r["explain"]


async def test_audit_trail_records_actor(admin):
    p = await make_profile(admin)
    sub = await make_subscriber(admin, p["id"])
    await admin.post(f"/api/v1/subscribers/{sub['subscriber']['id']}/renew", json={"days": 7})
    rows = (await admin.get(f"/api/v1/audit?subject=subscriber:{sub['subscriber']['id']}")).json()
    assert {r["action"] for r in rows} >= {"subscriber.create", "subscriber.renew"}
    assert all(r["actor"] == "atlas:mhamad" for r in rows)


async def test_daily_reset_refills_daily_quota(admin, session_factory, db_session):
    p = await make_profile(admin, quota_rxtx_daily=GB)
    sub = await make_subscriber(admin, p["id"])
    await login(admin)
    await interim(admin, rx=GB, tx=0)
    r = await admin.post("/radius/authorize", json={"username": "ali", "nas_ip_address": "127.0.0.1",
                                                    "calling_station_id": "AA:BB:CC:00:00:01"})
    assert r.json()["reply:Reply-Message"] == "gate:traffic"
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    async with session_factory() as db:
        out = await worker.reset_quotas(db, get_redis(), tomorrow)
        await db.commit()
    assert out["daily"] == 1
    detail = (await admin.get(f"/api/v1/subscribers/{sub['subscriber']['id']}")).json()
    assert detail["quotas"][0]["remaining"] == GB
