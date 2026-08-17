"""OpenSSL/CryptoJS-compatible AES envelope, matching SAS4's documented
{"payload": "<ciphertext>"} scheme: `CryptoJS.AES.encrypt(plaintext,
passphrase)` on the vendor's side produces base64("Salted__" + 8-byte-salt +
ciphertext), using OpenSSL's legacy EVP_BytesToKey (MD5, 1 iteration) to
derive a 32-byte key + 16-byte IV from the passphrase and salt.

This is legacy, deliberately weak key derivation (MD5, single iteration) —
that's the vendor's scheme, not a choice being endorsed here. Per Part C,
this pattern is quarantined to the one-time import and is not a precedent
for anything else in this codebase; the passphrase itself is published in
the vendor's own docs, so this "encryption" is transport obfuscation, not
a security boundary this platform is relying on.
"""

import base64
import hashlib

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

_SALT_PREFIX = b"Salted__"
_SALT_LEN = 8
_KEY_LEN = 32  # AES-256
_IV_LEN = 16


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    """OpenSSL's legacy EVP_BytesToKey with MD5, 1 iteration — what
    CryptoJS.AES.encrypt(str, passphrase) uses under the hood when given a
    plain string passphrase (as opposed to a raw key/IV pair).
    """
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        block = hashlib.md5(block + passphrase + salt).digest()
        derived += block
    return derived[:key_len], derived[key_len : key_len + iv_len]


def decrypt_openssl(ciphertext_b64: str, passphrase: str) -> str:
    raw = base64.b64decode(ciphertext_b64)
    if raw[:8] != _SALT_PREFIX:
        raise ValueError("not an OpenSSL-format 'Salted__' envelope")

    salt = raw[8:16]
    ciphertext = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode(), salt, _KEY_LEN, _IV_LEN)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return plaintext.decode()


def encrypt_openssl(plaintext: str, passphrase: str, salt: bytes | None = None) -> str:
    """Used for POST request bodies, which SAS4 expects wrapped the same way
    as responses: {"payload": encrypt_openssl(json.dumps(body), passphrase)}.
    `salt` is exposed only for deterministic tests — real calls must never
    pass it, so a fresh random salt is generated per encryption.
    """
    if salt is None:
        import os

        salt = os.urandom(_SALT_LEN)

    key, iv = _evp_bytes_to_key(passphrase.encode(), salt, _KEY_LEN, _IV_LEN)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(plaintext.encode(), AES.block_size))

    return base64.b64encode(_SALT_PREFIX + salt + ciphertext).decode()
