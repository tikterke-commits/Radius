"""RFC 5176 Dynamic Authorization client: CoA-Request and Disconnect-Request
toward a MikroTik NAS (`/radius incoming set accept=yes port=3799`).

RouterOS only accepts CoA from an address configured under /radius, so the
engine must send from the same host IP the NAS knows FreeRADIUS by — true
when backend/worker and freeradius run on the same docker host.

pyrad is synchronous; sends run in a worker thread so the event loop (and
with it every in-flight authorize) never blocks on a slow NAS.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pyrad import packet
from pyrad.client import Client, Timeout
from pyrad.dictionary import Dictionary

logger = logging.getLogger(__name__)

DICTIONARY_PATH = Path(__file__).with_name("dictionary")

_CODE_NAMES = {
    packet.CoAACK: "CoA-ACK",
    packet.CoANAK: "CoA-NAK",
    packet.DisconnectACK: "Disconnect-ACK",
    packet.DisconnectNAK: "Disconnect-NAK",
}

_dictionary: Dictionary | None = None


def dictionary() -> Dictionary:
    global _dictionary
    if _dictionary is None:
        _dictionary = Dictionary(str(DICTIONARY_PATH))
    return _dictionary


def known_attribute(name: str) -> bool:
    return name in dictionary().attributes


@dataclass(frozen=True)
class CoAResult:
    ok: bool
    response: str  # CoA-ACK / CoA-NAK / Disconnect-ACK / Disconnect-NAK / timeout / error
    error_cause: str | None = None
    detail: str | None = None
    sent: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "response": self.response, "error_cause": self.error_cause,
                "detail": self.detail, "sent": self.sent}


class CoASender(Protocol):
    async def coa(self, nas_ip: str, port: int, secret: str, attributes: dict) -> CoAResult: ...
    async def disconnect(self, nas_ip: str, port: int, secret: str, attributes: dict) -> CoAResult: ...


class PyradCoASender:
    def __init__(self, timeout_s: float = 2.0, retries: int = 1):
        self.timeout_s = timeout_s
        self.retries = max(1, retries + 1)  # pyrad counts attempts, settings count retries

    def _send(self, code: int, nas_ip: str, port: int, secret: str, attributes: dict) -> CoAResult:
        sent = {k: v for k, v in attributes.items() if v is not None}
        try:
            client = Client(server=nas_ip, coaport=port, secret=secret.encode(), dict=dictionary(),
                            retries=self.retries, timeout=self.timeout_s)
            req = client.CreateCoAPacket(code=code)
            for name, value in sent.items():
                req[name] = value
            reply = client.SendPacket(req)
        except Timeout:
            return CoAResult(ok=False, response="timeout", sent=sent)
        except Exception as exc:  # malformed attribute, socket error, unknown dictionary name
            logger.exception("CoA send to %s failed", nas_ip)
            return CoAResult(ok=False, response="error", detail=str(exc), sent=sent)

        name = _CODE_NAMES.get(reply.code, f"code-{reply.code}")
        cause = None
        if "Error-Cause" in reply:
            cause = str(reply["Error-Cause"][0])
        ok = reply.code in (packet.CoAACK, packet.DisconnectACK)
        return CoAResult(ok=ok, response=name, error_cause=cause, sent=sent)

    async def coa(self, nas_ip: str, port: int, secret: str, attributes: dict) -> CoAResult:
        return await asyncio.to_thread(self._send, packet.CoARequest, nas_ip, port, secret, attributes)

    async def disconnect(self, nas_ip: str, port: int, secret: str, attributes: dict) -> CoAResult:
        return await asyncio.to_thread(self._send, packet.DisconnectRequest, nas_ip, port, secret, attributes)
