"""Client for the legacy SAS4 (Snono Systems) admin API, per Part F's
confirmed semantics. Used only by the one-time import — never called at
runtime by the platform.

Confirmed:
- Laravel app, routes under /admin/api/index.php/api/..., JWT Bearer on
  every endpoint.
- POST bodies AES-encrypted, wrapped as {"payload": "<ciphertext>"}
  (see import_legacy/crypto.py for the envelope format).
- Errors return HTTP 200 with a body code of -1 — HTTP status is not a
  success signal, every response body must be inspected.
- Laravel paginator: page, count, sortBy, search, and a direction parameter
  spelled "direction" in the vendor's Python sample but "diraction" in
  their published docs. This client sends BOTH on every paginated request
  (harmless if the API ignores the one it doesn't recognize) rather than
  guessing — DIRECTION_PARAM_CONFIRMED below records which one actually
  sorted, once that's verified against the live instance; it's
  deliberately left False until then rather than assumed.
- Pages until next_page_url is null; total is not trusted.

Not confirmed, and NOT to be assumed: whether response bodies are also
AES-wrapped like requests, or plain JSON. This client tries plain JSON
first and falls back to decrypting a top-level "payload" key if present —
whichever branch actually fires against the live instance should be
recorded back into this docstring, per Part F's "where docs and database
disagree, trust the database and note it."
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from import_legacy.crypto import decrypt_openssl, encrypt_openssl

logger = logging.getLogger(__name__)

DIRECTION_PARAM_CONFIRMED = False  # flip once verified against the live instance; see docstring


class Sas4ApiError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"SAS4 API error {code}: {message}")


class Sas4Client:
    def __init__(
        self,
        base_url: str,
        jwt_token: str,
        aes_passphrase: str,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._jwt_token = jwt_token
        self._aes_passphrase = aes_passphrase
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt_token}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = encrypt_openssl(json.dumps(body), self._aes_passphrase)
        resp = await self._http.post(
            f"{self._base_url}{path}", json={"payload": payload}, headers=self._headers()
        )
        # Per Part F: HTTP status is never the success signal on this API.
        # Still raise on transport-level failure (connection refused, DNS,
        # TLS) — those aren't "the vendor's -1 convention," they're this
        # client not reaching the server at all.
        resp.raise_for_status()
        return self._decode_response(resp)

    async def _get(self, path: str) -> dict[str, Any]:
        resp = await self._http.get(f"{self._base_url}{path}", headers=self._headers())
        resp.raise_for_status()
        return self._decode_response(resp)

    def _decode_response(self, resp: httpx.Response) -> dict[str, Any]:
        data = resp.json()
        if "payload" in data and set(data.keys()) == {"payload"}:
            # Unconfirmed branch — see module docstring.
            logger.warning("SAS4 response was payload-wrapped; confirm this is expected")
            data = json.loads(decrypt_openssl(data["payload"], self._aes_passphrase))

        if data.get("code") == -1:
            raise Sas4ApiError(code=-1, message=data.get("message", "unknown error"))

        return data

    async def list_paginated(
        self, entity: str, count: int = 100, search: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Generic `index/<entity>` pagination (Part F: "index/<entity> is
        likely the generic list pattern — verify each against the live
        instance"). Pages until next_page_url is absent/null; never trusts
        `total`.
        """
        page = 1
        while True:
            body: dict[str, Any] = {"page": page, "count": count}
            if search:
                body["search"] = search
            # Sending both spellings rather than guessing which one the live
            # instance honors — see DIRECTION_PARAM_CONFIRMED above.
            body["direction"] = "asc"
            body["diraction"] = "asc"

            result = await self._post(f"/admin/api/index.php/api/index/{entity}", body)
            data = result.get("data", [])
            for item in data:
                yield item

            if not result.get("next_page_url"):
                return
            page += 1

    async def list_users(self, count: int = 100, search: str | None = None):
        async for user in self.list_paginated("user", count=count, search=search):
            yield user

    async def get_user_detail(self, user_id: str) -> dict[str, Any]:
        return await self._get(f"/admin/api/index.php/api/user/overview/{user_id}")
