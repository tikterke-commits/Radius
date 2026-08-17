"""Credential hashing per the A5 decision documented in
docs/00-part-h-design.md §5: PAP-only, Argon2id, no NT-hash, no reversible
storage of any kind. MS-CHAPv2/CHAP are not supported by this platform.

This module is the only place a plaintext password should ever exist in
memory, and only for the duration of a single verify/hash call — never
logged, never returned, never stored.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

_hasher = PasswordHasher()

CURRENT_ALGO_VERSION = 1


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Returns False on mismatch or on a malformed/foreign hash. Never raises
    for bad input — an authorize()/authenticate() caller on the RADIUS hot
    path should get a clean boolean, not an exception to handle.
    """
    try:
        _hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with weaker parameters than current defaults
    (e.g. after an Argon2 parameter bump). Check on successful login, rehash
    if true — standard credential-hygiene practice, not urgent enough to
    force a rotation event on its own.
    """
    return _hasher.check_needs_rehash(password_hash)
