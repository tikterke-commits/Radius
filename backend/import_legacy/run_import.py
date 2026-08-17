"""One-time SAS4 -> platform import (Part F). Flag-gated behind
settings.import_enabled — refuses to run otherwise, so it can't be
accidentally invoked once the platform is live. Idempotent: matched and
upserted by legacy_sas4_id, safe to re-run after a partial failure.

Scope of this pass: subscribers, credentials (remediated per A5), plans
(provisioned from profile_map.yaml's normalized tiers), subscriptions, and
monthly rxtx/uptime quota state seeded from the legacy detail endpoint's
remaining_* fields. Part F's full module enumeration (IP pools, NAS
devices, managers/permission groups, invoices, payments, cards/vouchers,
add-ons, sessions, tickets) is broader than this; those are left as
follow-up importers using the same Sas4Client and the same
idempotent-upsert pattern, not built here — flagging the gap rather than
half-building all of them shallowly.

Also explicitly not handled: `daily_traffic_details` on the list endpoint.
Part F names the field but its shape isn't documented anywhere available to
this session, so daily quota rows are left unset (unlimited) at import time
rather than guessed at — filling them in is follow-up work once that field
has been inspected against the live instance.

Usage:
    IMPORT_ENABLED=true python -m import_legacy.run_import \\
        --base-url https://sas4.example.com --jwt-token ... \\
        --aes-passphrase ... --tenant-id ...
"""

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models.credential import Credential
from app.models.plan import Plan, Subscription
from app.models.quota import QuotaKind, QuotaPeriod, QuotaState
from app.models.subscriber import Subscriber
from app.security.credentials import hash_password
from import_legacy.profile_normalizer import ProfileNormalizer, UnmappedProfileError
from import_legacy.sas4_client import Sas4Client

logger = logging.getLogger("import_legacy")

# Staged rotation deadline per A5 — this only sets the deadline on the
# credential row; nothing in this module enforces it. Actually forcing
# rotation (e.g. blocking auth past the deadline) is a separate, explicitly
# confirmed operator action, not something the import triggers on its own.
ROTATION_WINDOW_DAYS = 30


async def run(client: Sas4Client, tenant_id: uuid.UUID, normalizer: ProfileNormalizer) -> None:
    if not settings.import_enabled:
        raise RuntimeError(
            "IMPORT_ENABLED is not true — refusing to run. This is deliberate: the import "
            "module is flag-gated per Part F and must not run against a live platform by accident."
        )

    imported, skipped, failed = 0, 0, 0

    async with SessionLocal() as db:
        plans_by_tier = await _ensure_plans(db, tenant_id, normalizer)
        await db.commit()

        async for legacy_user in client.list_users():
            try:
                await _import_one_subscriber(db, tenant_id, client, legacy_user, normalizer, plans_by_tier)
                await db.commit()
                imported += 1
            except UnmappedProfileError as e:
                await db.rollback()
                logger.error("skipping %s: %s", legacy_user.get("username"), e)
                skipped += 1
            except Exception:
                await db.rollback()
                logger.exception("failed importing %s", legacy_user.get("username"))
                failed += 1

    logger.info(
        "import complete: %d imported, %d skipped (unmapped profile), %d failed", imported, skipped, failed
    )


async def _ensure_plans(
    db: AsyncSession, tenant_id: uuid.UUID, normalizer: ProfileNormalizer
) -> dict[str, Plan]:
    plans_by_tier: dict[str, Plan] = {}
    for tier in normalizer.all_tiers():
        existing = (
            await db.execute(
                select(Plan).where(
                    Plan.tenant_id == tenant_id, Plan.profile_name_normalized == tier.normalized_name
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Plan(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=tier.normalized_name,
                profile_name_normalized=tier.normalized_name,
                rx_limit_bps=tier.rx_limit_bps,
                tx_limit_bps=tier.tx_limit_bps,
                fup_tiers=[],
            )
            db.add(existing)
            await db.flush()
        plans_by_tier[tier.normalized_name] = existing
    return plans_by_tier


async def _import_one_subscriber(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    client: Sas4Client,
    legacy_user: dict,
    normalizer: ProfileNormalizer,
    plans_by_tier: dict[str, Plan],
) -> None:
    legacy_id = str(legacy_user["id"])

    profile_name = (legacy_user.get("profile_details") or {}).get("name", "")
    tier = normalizer.resolve(profile_name)  # raises UnmappedProfileError; caller skips this subscriber
    plan = plans_by_tier[tier.normalized_name]

    subscriber = (
        await db.execute(select(Subscriber).where(Subscriber.legacy_sas4_id == legacy_id))
    ).scalar_one_or_none()
    if subscriber is None:
        subscriber = Subscriber(id=uuid.uuid4(), tenant_id=tenant_id, legacy_sas4_id=legacy_id)
        db.add(subscriber)

    subscriber.username = legacy_user["username"]
    subscriber.firstname = legacy_user.get("firstname")
    subscriber.lastname = legacy_user.get("lastname")
    subscriber.company = legacy_user.get("company")
    subscriber.phone = legacy_user.get("phone")
    subscriber.email = legacy_user.get("email")
    subscriber.address = legacy_user.get("address")
    subscriber.city = legacy_user.get("city")
    subscriber.notes = legacy_user.get("notes")
    subscriber.national_id = legacy_user.get("national_id")
    subscriber.contract_id = legacy_user.get("contract_id")
    subscriber.static_ip = legacy_user.get("static_ip") or None
    subscriber.enabled = bool(legacy_user.get("enabled", True))
    await db.flush()

    # Detail endpoint carries the (cleartext, per A5's compromise assumption)
    # password and remaining_* quota fields the list endpoint doesn't.
    detail = await client.get_user_detail(legacy_id)
    await _remediate_credential(db, subscriber.id, detail.get("password"))
    await _upsert_subscription(db, subscriber.id, plan.id, detail)
    await _upsert_monthly_quota(db, subscriber.id, detail)


async def _upsert_subscription(db: AsyncSession, subscriber_id: uuid.UUID, plan_id: uuid.UUID, detail: dict) -> None:
    subscription = (
        await db.execute(select(Subscription).where(Subscription.subscriber_id == subscriber_id))
    ).scalar_one_or_none()

    expiration = _parse_legacy_datetime(detail.get("expiration"))
    next_change_at = _parse_legacy_datetime(detail.get("next_profile_change"))

    if subscription is None:
        subscription = Subscription(
            subscriber_id=subscriber_id,
            plan_id=plan_id,
            started_at=datetime.now(timezone.utc),
        )
        db.add(subscription)

    subscription.plan_id = plan_id
    subscription.expiration = expiration
    subscription.next_profile_change_at = next_change_at


async def _upsert_monthly_quota(db: AsyncSession, subscriber_id: uuid.UUID, detail: dict) -> None:
    field_by_kind = {
        QuotaKind.RX: detail.get("remaining_rx"),
        QuotaKind.TX: detail.get("remaining_tx"),
        QuotaKind.RXTX: detail.get("remaining_rxtx"),
        QuotaKind.UPTIME: detail.get("remaining_uptime"),
    }
    now = datetime.now(timezone.utc)
    for kind, remaining in field_by_kind.items():
        if remaining is None:
            continue  # unlimited for this kind — no row means "no quota configured"

        row = (
            await db.execute(
                select(QuotaState).where(
                    QuotaState.subscriber_id == subscriber_id,
                    QuotaState.kind == kind,
                    QuotaState.period == QuotaPeriod.MONTHLY,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = QuotaState(
                subscriber_id=subscriber_id,
                kind=kind,
                period=QuotaPeriod.MONTHLY,
                remaining=int(remaining),
                resets_at=now + timedelta(days=30),  # placeholder cadence; real billing-cycle
                                                       # anchor needs the plan's actual cycle date
                updated_at=now,
            )
            db.add(row)
        else:
            row.remaining = int(remaining)
            row.updated_at = now


async def _remediate_credential(db: AsyncSession, subscriber_id: uuid.UUID, legacy_password: str | None) -> None:
    """Per A5: every imported credential is compromised by assumption. Hash
    immediately with Argon2id, never persist the plaintext, and flag for
    staged rotation rather than trusting the imported hash's strength —
    there isn't one, SAS4 stores it recoverable.
    """
    if not legacy_password:
        return

    credential = (
        await db.execute(select(Credential).where(Credential.subscriber_id == subscriber_id))
    ).scalar_one_or_none()

    password_hash = hash_password(legacy_password)
    del legacy_password  # not scrubbed from memory (Python can't guarantee that), but not referenced again

    if credential is None:
        credential = Credential(subscriber_id=subscriber_id, password_hash=password_hash)
        db.add(credential)
    else:
        credential.password_hash = password_hash

    credential.imported_from_sas4 = True
    credential.forced_rotation = True
    credential.rotation_deadline = datetime.now(timezone.utc) + timedelta(days=ROTATION_WINDOW_DAYS)


def _parse_legacy_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # Exact legacy datetime format (ISO-8601? "Y-m-d H:i:s"?) isn't confirmed
    # against the live instance — ISO format attempted first since that's
    # Laravel's common default, with a Y-m-d H:i:s fallback. Whichever
    # branch actually fires should be recorded here once verified, per Part
    # F's "where docs and database disagree, trust the database and note it."
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--jwt-token", required=True)
    parser.add_argument("--aes-passphrase", required=True)
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()

    client = Sas4Client(base_url=args.base_url, jwt_token=args.jwt_token, aes_passphrase=args.aes_passphrase)
    normalizer = ProfileNormalizer()
    try:
        await run(client, args.tenant_id, normalizer)
    finally:
        await client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except RuntimeError as e:
        print(f"refusing to run: {e}", file=sys.stderr)
        sys.exit(1)
