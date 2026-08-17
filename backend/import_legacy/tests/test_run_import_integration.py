"""Runs the actual import against the real local Postgres (see
tests/integration/ for why this sandbox can do that without docker: both
postgresql-16 and redis-server are installed as local binaries here). The
SAS4 side is a MockTransport fake — this sandbox has no access to a live
SAS4 instance — but everything from the HTTP client's AES envelope handling
down through the ORM writes is real production code, not a mock.
"""

import json
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app import models  # noqa: F401
from app.models.credential import Credential
from app.models.plan import Subscription
from app.models.quota import QuotaKind, QuotaPeriod, QuotaState
from app.models.subscriber import Subscriber
from app.models.tenant import Tenant
from app.security.credentials import verify_password
from import_legacy.crypto import encrypt_openssl
from import_legacy.profile_normalizer import DEFAULT_MAP_PATH, ProfileNormalizer
from import_legacy.sas4_client import Sas4Client

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://radius_test:radius_test@127.0.0.1:5432/radius_test"
)
AES_PASSPHRASE = "test-import-passphrase"

LEGACY_USERS = [
    {
        "id": "sas4-1",
        "username": "alice",
        "firstname": "Alice",
        "profile_details": {"name": "10M Basic Plan"},
        "enabled": True,
    },
    {
        "id": "sas4-2",
        "username": "bob",
        "firstname": "Bob",
        "profile_details": {"name": "Totally Unknown Legacy Tier"},  # deliberately unmapped
        "enabled": True,
    },
]

LEGACY_DETAILS = {
    "sas4-1": {
        "password": "alice-plaintext-legacy-password",
        "remaining_rx": 5_000_000_000,
        "remaining_tx": 5_000_000_000,
        "remaining_rxtx": 10_000_000_000,
        "remaining_uptime": 360_000,
        "expiration": "2027-01-01T00:00:00",
    },
    "sas4-2": {"password": "bob-password"},
}


def _mock_sas4_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/index/user"):
            return httpx.Response(200, json={"data": LEGACY_USERS, "next_page_url": None})
        for legacy_id, detail in LEGACY_DETAILS.items():
            if path.endswith(f"/user/overview/{legacy_id}"):
                return httpx.Response(200, json=detail)
        return httpx.Response(404, json={"code": -1, "message": "not found"})

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(db_engine):
    async with db_engine.begin() as conn:
        table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.exec_driver_sql(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE")
    yield


@pytest_asyncio.fixture
async def tenant_id(db_engine):
    SessionLocal = async_sessionmaker(db_engine, expire_on_commit=False)
    tid = uuid.uuid4()
    async with SessionLocal() as session:
        session.add(Tenant(id=tid, name="Data Plus Lebanon"))
        await session.commit()
    return tid


@pytest.fixture(autouse=True)
def enable_import(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "import_enabled", True)


@pytest.fixture(autouse=True)
def use_test_database(monkeypatch):
    # run_import.py imports app.db.SessionLocal at module scope, which is
    # bound to whatever DATABASE_URL the app's own settings resolved at
    # import time — repoint it at the test database for this test run.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as app_db
    import import_legacy.run_import as run_import_module

    test_engine = create_async_engine(TEST_DATABASE_URL)
    test_session_local = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(app_db, "SessionLocal", test_session_local)
    monkeypatch.setattr(run_import_module, "SessionLocal", test_session_local)


async def test_import_creates_mapped_subscriber_and_skips_unmapped_one(tenant_id):
    from import_legacy.run_import import run

    client = Sas4Client(
        base_url="https://sas4.example.test",
        jwt_token="fake",
        aes_passphrase=AES_PASSPHRASE,
        http_client=httpx.AsyncClient(transport=_mock_sas4_transport()),
    )
    normalizer = ProfileNormalizer(DEFAULT_MAP_PATH)

    await run(client, tenant_id, normalizer)

    engine = create_async_engine(TEST_DATABASE_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as db:
        alice = (
            await db.execute(select(Subscriber).where(Subscriber.username == "alice"))
        ).scalar_one_or_none()
        bob = (
            await db.execute(select(Subscriber).where(Subscriber.username == "bob"))
        ).scalar_one_or_none()

        assert alice is not None, "mapped-profile subscriber should be imported"
        assert bob is None, "unmapped-profile subscriber should be skipped, not guessed at"
    await engine.dispose()


async def test_import_remediates_credential_never_stores_plaintext(tenant_id):
    from import_legacy.run_import import run

    client = Sas4Client(
        base_url="https://sas4.example.test",
        jwt_token="fake",
        aes_passphrase=AES_PASSPHRASE,
        http_client=httpx.AsyncClient(transport=_mock_sas4_transport()),
    )
    normalizer = ProfileNormalizer(DEFAULT_MAP_PATH)
    await run(client, tenant_id, normalizer)

    engine = create_async_engine(TEST_DATABASE_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as db:
        alice = (
            await db.execute(select(Subscriber).where(Subscriber.username == "alice"))
        ).scalar_one()
        cred = (
            await db.execute(select(Credential).where(Credential.subscriber_id == alice.id))
        ).scalar_one()

        assert cred.password_hash != "alice-plaintext-legacy-password"
        assert cred.password_hash.startswith("$argon2id$")
        assert verify_password("alice-plaintext-legacy-password", cred.password_hash)
        assert cred.imported_from_sas4 is True
        assert cred.forced_rotation is True
        assert cred.rotation_deadline is not None
    await engine.dispose()


async def test_import_seeds_quota_from_legacy_remaining_fields(tenant_id):
    from import_legacy.run_import import run

    client = Sas4Client(
        base_url="https://sas4.example.test",
        jwt_token="fake",
        aes_passphrase=AES_PASSPHRASE,
        http_client=httpx.AsyncClient(transport=_mock_sas4_transport()),
    )
    normalizer = ProfileNormalizer(DEFAULT_MAP_PATH)
    await run(client, tenant_id, normalizer)

    engine = create_async_engine(TEST_DATABASE_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as db:
        alice = (
            await db.execute(select(Subscriber).where(Subscriber.username == "alice"))
        ).scalar_one()
        rxtx = (
            await db.execute(
                select(QuotaState).where(
                    QuotaState.subscriber_id == alice.id,
                    QuotaState.kind == QuotaKind.RXTX,
                    QuotaState.period == QuotaPeriod.MONTHLY,
                )
            )
        ).scalar_one()
        assert rxtx.remaining == 10_000_000_000

        sub = (
            await db.execute(select(Subscription).where(Subscription.subscriber_id == alice.id))
        ).scalar_one()
        assert sub.expiration.year == 2027
    await engine.dispose()


async def test_import_is_idempotent_on_rerun(tenant_id):
    from import_legacy.run_import import run

    async def fresh_client():
        return Sas4Client(
            base_url="https://sas4.example.test",
            jwt_token="fake",
            aes_passphrase=AES_PASSPHRASE,
            http_client=httpx.AsyncClient(transport=_mock_sas4_transport()),
        )

    normalizer = ProfileNormalizer(DEFAULT_MAP_PATH)
    await run(await fresh_client(), tenant_id, normalizer)
    await run(await fresh_client(), tenant_id, normalizer)  # re-run, same legacy data

    engine = create_async_engine(TEST_DATABASE_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as db:
        alices = (
            await db.execute(select(Subscriber).where(Subscriber.username == "alice"))
        ).scalars().all()
        assert len(alices) == 1, "re-running the import must not create duplicate subscribers"
    await engine.dispose()
