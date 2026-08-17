"""Request/response shapes for the rlm_rest calls FreeRADIUS makes into this
backend, matched against rlm_rest's actual (verified against the module's own
config comments, not assumed) contract:

- The module's return code comes from the HTTP status code, not a body field:
  authorize/authenticate: 2xx -> ok/updated (body processed), 401 -> reject
  (body still processed, so a Reply-Message can ride along), 5xx -> fail.
  accounting/post-auth: 204/2xx -> ok, 5xx -> fail.
- A JSON response's top-level keys are attribute references with an explicit
  list prefix ("reply:Attribute-Name", "control:Attribute-Name") — there is
  no default list, so every key here must be prefixed.

See radius/mods-enabled/rest for the FreeRADIUS-side half of this contract.
"""

from pydantic import BaseModel


class AuthorizeRequest(BaseModel):
    username: str
    nas_ip_address: str
    nas_port_id: str | None = None
    calling_station_id: str | None = None  # MAC, for mac_binding enforcement


class AuthenticateRequest(BaseModel):
    username: str
    password: str  # PAP cleartext, RADIUS-shared-secret-protected on the wire; never logged, never stored


class AccountingRequest(BaseModel):
    username: str
    acct_status_type: str  # Start | Interim-Update | Stop
    acct_session_id: str
    nas_ip_address: str
    acct_session_time: int = 0
    acct_input_octets: int = 0   # what the NAS received from the subscriber -> rx
    acct_output_octets: int = 0  # what the NAS sent to the subscriber -> tx
    acct_terminate_cause: str | None = None


class PostAuthRequest(BaseModel):
    username: str
    packet_type: str  # Access-Accept | Access-Reject
    nas_ip_address: str
