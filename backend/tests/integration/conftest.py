"""Fixtures for the real-backend integration tests: an actual Postgres and an
actual Redis, not mocks. Requires TEST_DATABASE_URL and TEST_REDIS_URL (or
their defaults below) to point at reachable instances — see
tests/integration/README.md for how this session set that up locally
without docker.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app import models  # noqa: F401 registers tables
from app.models.credential import Credential
from app.models.plan import Plan, Subscription
from app.models.quota import QuotaKind, QuotaPeriod, QuotaState
from app.models.subscriber import Subscriber
from app.models.tenant import Tenant
from app.security.credentials import hash_password

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://radius_test:radius_test@127.0.0.1:5432/radius_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/1")


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    SessionLocal = async_sessionmaker(db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(db_engine):
    """Truncate everything between tests rather than recreating the schema —
    the schema itself is validated once, at migration-apply time (see the
    session's own `alembic upgrade head` run against this same database)."""
    async with db_engine.begin() as conn:
        table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.exec_driver_sql(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE")
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_redis():
    from redis.asyncio import from_url

    client = from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    """An httpx AsyncClient wired directly to the real FastAPI app, with
    get_db overridden to hand out this test's transactional session and
    REDIS_URL pointed at the test Redis DB — everything else (routing,
    the policy engine, the ORM queries) is the real production code path.
    """
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    from app import redis_client
    redis_client._redis = None  # force re-init against TEST_REDIS_URL

    from app.config import settings
    monkeypatch.setattr(settings, "redis_url", TEST_REDIS_URL)

    from app.db import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seeded_subscriber(db_session):
    """A subscriber on a small plan with a tight monthly rxtx quota, so the
    exhaustion test doesn't need thousands of accounting calls to trigger.
    """
    tenant = Tenant(id=uuid.uuid4(), name="Data Plus Lebanon")
    db_session.add(tenant)
    await db_session.flush()

    plan = Plan(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Test 10M",
        profile_name_normalized="test-10m",
        rx_limit_bps=10_000_000,
        tx_limit_bps=10_000_000,
        quota_rxtx_monthly=1_000_000,  # 1 MB — small on purpose, see docstring
        fup_tiers=[],
        default_concurrent_sessions=1,
    )
    db_session.add(plan)
    await db_session.flush()

    subscriber = Subscriber(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        username="alice",
        enabled=True,
    )
    db_session.add(subscriber)
    await db_session.flush()

    db_session.add(Credential(
        subscriber_id=subscriber.id,
        password_hash=hash_password("correct-horse-battery-staple"),
    ))

    db_session.add(Subscription(
        subscriber_id=subscriber.id,
        plan_id=plan.id,
        started_at=datetime.now(timezone.utc) - timedelta(days=1),
        expiration=datetime.now(timezone.utc) + timedelta(days=29),
    ))

    now = datetime.now(timezone.utc)
    db_session.add(QuotaState(
        subscriber_id=subscriber.id,
        kind=QuotaKind.RXTX,
        period=QuotaPeriod.MONTHLY,
        remaining=1_000_000,
        resets_at=now + timedelta(days=29),
        updated_at=now,
    ))

    await db_session.commit()
    return subscriber, plan
