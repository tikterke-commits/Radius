"""Thin wrapper around clickhouse-connect. Writes here are best-effort from
the RADIUS hot path's perspective: a ClickHouse hiccup mirrors a lost
telemetry row, not a lost accounting decision — the Postgres quota_state
write (the thing that actually gates future authorize calls) must never be
made to depend on ClickHouse being up. Callers catch and log, never raise.
"""

import logging

import clickhouse_connect

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


def get_client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=_host(),
            port=8123,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_db,
        )
    return _client


def _host() -> str:
    return settings.clickhouse_url.replace("http://", "").replace("https://", "").split(":")[0]


def insert_radacct_event(
    event_ts, subscriber_id, username, nas_ip, acct_session_id, status_type,
    session_time_s, input_octets, output_octets, terminate_cause=""
) -> None:
    try:
        get_client().insert(
            "radacct_events",
            [[
                event_ts, subscriber_id, username, nas_ip, acct_session_id, status_type,
                session_time_s, input_octets, output_octets, terminate_cause,
            ]],
            column_names=[
                "event_ts", "subscriber_id", "username", "nas_ip", "acct_session_id",
                "status_type", "session_time_s", "input_octets", "output_octets", "terminate_cause",
            ],
        )
    except Exception:
        logger.exception("clickhouse radacct_events insert failed for session %s", acct_session_id)
