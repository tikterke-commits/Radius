"""Integration test against the real FastAPI app, a real Postgres, and a real
Redis (see conftest.py) — covers auth -> accounting -> quota exhaustion, the
part of the required "auth -> accounting -> quota exhaustion -> CoA ->
reconnect" scenario (docs/00-part-h-design.md / the build prompt's working
rules) that's actually runnable in this environment.

NOT covered here, and NOT run in this session: the CoA -> reconnect half,
which needs a real FreeRADIUS process speaking the wire protocol plus a
simulated MikroTik NAS responding to CoA-Request per RFC 5176. That needs
containerized FreeRADIUS — no docker daemon is available in this sandbox
(confirmed: `docker ps` fails with "no such file or directory" on the
socket). tests/integration/test_coa_reconnect_SKELETON.py sketches that test
structurally so it's ready to fill in and run in CI or on a real docker
host; it's not wired into the default test run.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_authorize_healthy_subscriber_returns_plan_rate(client, seeded_subscriber):
    subscriber, plan = seeded_subscriber

    resp = await client.post(
        "/radius/authorize",
        json={"username": "alice", "nas_ip_address": "10.140.140.1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply:Mikrotik-Rate-Limit"] == f"{plan.rx_limit_bps}/{plan.tx_limit_bps}"


async def test_authorize_unknown_username_rejects(client, seeded_subscriber):
    resp = await client.post(
        "/radius/authorize",
        json={"username": "nobody", "nas_ip_address": "10.140.140.1"},
    )
    assert resp.status_code == 401


async def test_authenticate_correct_password_ok(client, seeded_subscriber):
    resp = await client.post(
        "/radius/authenticate",
        json={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    assert resp.status_code == 200


async def test_authenticate_wrong_password_rejects(client, seeded_subscriber):
    resp = await client.post(
        "/radius/authenticate",
        json={"username": "alice", "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_accounting_decrements_quota(client, seeded_subscriber, db_session):
    from sqlalchemy import select
    from app.models.quota import QuotaState

    subscriber, _ = seeded_subscriber

    resp = await client.post(
        "/radius/accounting",
        json={
            "username": "alice",
            "acct_status_type": "Interim-Update",
            "acct_session_id": "sess-1",
            "nas_ip_address": "10.140.140.1",
            "acct_session_time": 60,
            "acct_input_octets": 100_000,
            "acct_output_octets": 50_000,
        },
    )
    assert resp.status_code == 204

    row = (
        await db_session.execute(
            select(QuotaState).where(QuotaState.subscriber_id == subscriber.id)
        )
    ).scalar_one()
    # 1,000,000 - (100,000 + 50,000) = 850,000
    assert row.remaining == 850_000


async def test_quota_exhaustion_flips_authorize_to_reject(client, seeded_subscriber, db_session):
    """The core scenario: usage accumulates via accounting until the plan's
    1MB monthly rxtx quota is gone, and the next authorize call for the same
    subscriber is rejected specifically because of the traffic gate — not
    because of a coincidental other failure.
    """
    subscriber, _ = seeded_subscriber

    # Consume the full 1,000,000 byte quota in one interim update.
    resp = await client.post(
        "/radius/accounting",
        json={
            "username": "alice",
            "acct_status_type": "Interim-Update",
            "acct_session_id": "sess-1",
            "nas_ip_address": "10.140.140.1",
            "acct_session_time": 300,
            "acct_input_octets": 600_000,
            "acct_output_octets": 400_000,
        },
    )
    assert resp.status_code == 204

    resp = await client.post(
        "/radius/authorize",
        json={"username": "alice", "nas_ip_address": "10.140.140.1"},
    )
    assert resp.status_code == 401
    assert "traffic" in resp.json()["reply:Reply-Message"]


async def test_accounting_uses_delta_not_cumulative_across_interim_updates(
    client, seeded_subscriber, db_session
):
    """RADIUS Interim-Update packets report session-cumulative octets. A
    naive implementation that treated each packet's octets as a fresh
    decrement (instead of diffing against the last-seen cumulative value)
    would double- or triple-count usage across a session's interim updates.
    This is exactly the bug RadiusSession exists to prevent.
    """
    from sqlalchemy import select
    from app.models.quota import QuotaState

    subscriber, _ = seeded_subscriber

    for cumulative_input in (100_000, 250_000, 400_000):  # session running total, not deltas
        resp = await client.post(
            "/radius/accounting",
            json={
                "username": "alice",
                "acct_status_type": "Interim-Update",
                "acct_session_id": "sess-1",
                "nas_ip_address": "10.140.140.1",
                "acct_session_time": 60,
                "acct_input_octets": cumulative_input,
                "acct_output_octets": 0,
            },
        )
        assert resp.status_code == 204

    row = (
        await db_session.execute(
            select(QuotaState).where(QuotaState.subscriber_id == subscriber.id)
        )
    ).scalar_one()
    # Total real usage is 400,000 (the final cumulative value), not
    # 100,000 + 250,000 + 400,000 = 750,000 from double-counting.
    assert row.remaining == 1_000_000 - 400_000
