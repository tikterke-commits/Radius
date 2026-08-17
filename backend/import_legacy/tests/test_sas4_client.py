"""Exercises Sas4Client's pagination and error-handling logic against a fake
transport (httpx.MockTransport) rather than a live SAS4 instance, which this
session has no access to — see docs/00-part-h-design.md §0. This validates
the client's own logic (pagination termination, -1-code error handling,
sending both direction/diraction spellings) is correct; it cannot validate
that the real vendor API actually behaves the way Part F describes.
"""

import json

import httpx
import pytest

from import_legacy.crypto import encrypt_openssl
from import_legacy.sas4_client import Sas4ApiError, Sas4Client

PASSPHRASE = "test-passphrase"

pytestmark = pytest.mark.asyncio


def _make_client(handler) -> Sas4Client:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return Sas4Client(
        base_url="https://sas4.example.test",
        jwt_token="fake-jwt",
        aes_passphrase=PASSPHRASE,
        http_client=http_client,
    )


async def test_list_paginated_stops_when_next_page_url_is_absent():
    pages = {
        1: {"data": [{"id": 1}, {"id": 2}], "next_page_url": "https://x/page=2", "total": 999},
        2: {"data": [{"id": 3}], "next_page_url": None, "total": 999},
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert set(body.keys()) == {"payload"}
        calls.append(request)
        page = len(calls)
        return httpx.Response(200, json=pages[page])

    client = _make_client(handler)
    items = [item async for item in client.list_paginated("user")]

    assert [i["id"] for i in items] == [1, 2, 3]
    assert len(calls) == 2  # stopped because page 2 had no next_page_url, not because of `total`


async def test_list_paginated_ignores_wrong_total_and_trusts_next_page_url_only():
    # `total` claims way more than actually exists across the pages served —
    # per Part F, total must never be trusted for loop termination.
    pages = {
        1: {"data": [{"id": 1}], "next_page_url": None, "total": 10_000},
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=pages[1])

    client = _make_client(handler)
    items = [item async for item in client.list_paginated("user")]

    assert [i["id"] for i in items] == [1]
    assert len(calls) == 1


async def test_request_body_sends_both_direction_spellings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        from import_legacy.crypto import decrypt_openssl

        decrypted = json.loads(decrypt_openssl(envelope["payload"], PASSPHRASE))
        captured.update(decrypted)
        return httpx.Response(200, json={"data": [], "next_page_url": None})

    client = _make_client(handler)
    _ = [item async for item in client.list_paginated("user")]

    assert captured["direction"] == "asc"
    assert captured["diraction"] == "asc"


async def test_body_code_minus_one_raises_even_with_http_200():
    def handler(request: httpx.Request) -> httpx.Response:
        # HTTP 200 with an error code in the body — the exact anti-pattern
        # Part F describes and this client must not be fooled by.
        return httpx.Response(200, json={"code": -1, "message": "invalid session token"})

    client = _make_client(handler)
    with pytest.raises(Sas4ApiError) as exc_info:
        await client.get_user_detail("123")

    assert exc_info.value.code == -1
    assert "invalid session token" in exc_info.value.message


async def test_payload_wrapped_response_is_decrypted_as_fallback():
    inner = {"id": "123", "username": "legacy_user"}

    def handler(request: httpx.Request) -> httpx.Response:
        wrapped = encrypt_openssl(json.dumps(inner), PASSPHRASE)
        return httpx.Response(200, json={"payload": wrapped})

    client = _make_client(handler)
    result = await client.get_user_detail("123")

    assert result == inner
