from app.security.credentials import hash_password, needs_rehash, verify_password


def test_hash_is_not_plaintext():
    h = hash_password("hunter2")
    assert h != "hunter2"
    assert h.startswith("$argon2id$")


def test_verify_correct_password():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_wrong_password():
    h = hash_password("hunter2")
    assert verify_password("wrong-password", h) is False


def test_verify_malformed_hash_returns_false_not_exception():
    assert verify_password("hunter2", "not-a-real-hash") is False


def test_two_hashes_of_same_password_differ():
    # Argon2id salts per-hash; a shared default password (the live legacy
    # situation across most imported accounts) must not produce identical
    # stored hashes.
    h1 = hash_password("shared-default-password")
    h2 = hash_password("shared-default-password")
    assert h1 != h2
    assert verify_password("shared-default-password", h1)
    assert verify_password("shared-default-password", h2)


def test_needs_rehash_false_for_freshly_hashed_password():
    h = hash_password("hunter2")
    assert needs_rehash(h) is False
