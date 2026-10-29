"""A stand-in MikroTik for the CoA leg: a real UDP listener that decodes
RFC 5176 CoA-Request / Disconnect-Request with the shared secret, verifies the
Request Authenticator, records what it got, and answers ACK (or NAK when told
to). The engine's PyradCoASender talks to it exactly as it would to a router.
"""

import socket
import threading

from pyrad import packet

from app.actuation.coa import dictionary


class FakeNas:
    def __init__(self, secret: str = "nas-secret-123", nak: bool = False):
        self.secret = secret.encode()
        self.nak = nak
        self.received: list[dict] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            req = packet.CoAPacket(packet=data, secret=self.secret, dict=dictionary())
            attrs = {name: req[name][0] for name in req.keys() if isinstance(name, str)}
            self.received.append({
                "code": "disconnect" if req.code == packet.DisconnectRequest else "coa",
                "authenticator_ok": req.VerifyCoARequest(),
                "attributes": attrs,
            })
            reply = req.CreateReply()
            if self.nak:
                reply.code = packet.DisconnectNAK if req.code == packet.DisconnectRequest else packet.CoANAK
            self.sock.sendto(reply.ReplyPacket(), addr)
