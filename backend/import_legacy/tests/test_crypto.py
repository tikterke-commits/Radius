"""The known-answer vector here was generated independently with the real
openssl CLI (not this module), specifically so this test verifies
compatibility with the actual OpenSSL/CryptoJS envelope format rather than
just round-tripping against itself:

    $ echo -n '{"hello":"world"}' | openssl enc -aes-256-cbc -md md5 -a -salt \
        -pass pass:mysecretpassphrase
    U2FsdGVkX185cwZsymlkr3NtHcs8zqbvcJdDURt0JnSxCh07DHyqqB0COxHRoh/3

    $ echo <that> | openssl enc -aes-256-cbc -md md5 -a -d -salt \
        -pass pass:mysecretpassphrase
    {"hello":"world"}
"""

from import_legacy.crypto import decrypt_openssl, encrypt_openssl

KNOWN_CIPHERTEXT = "U2FsdGVkX185cwZsymlkr3NtHcs8zqbvcJdDURt0JnSxCh07DHyqqB0COxHRoh/3"
KNOWN_PLAINTEXT = '{"hello":"world"}'
PASSPHRASE = "mysecretpassphrase"


def test_decrypt_matches_real_openssl_known_answer_vector():
    assert decrypt_openssl(KNOWN_CIPHERTEXT, PASSPHRASE) == KNOWN_PLAINTEXT


def test_decrypt_wrong_passphrase_raises_rather_than_silently_returning_garbage():
    import pytest

    with pytest.raises(ValueError):
        decrypt_openssl(KNOWN_CIPHERTEXT, "wrong-passphrase")


def test_encrypt_then_decrypt_round_trips():
    plaintext = '{"username":"alice","action":"list"}'
    ciphertext = encrypt_openssl(plaintext, PASSPHRASE)
    assert decrypt_openssl(ciphertext, PASSPHRASE) == plaintext


def test_encrypt_with_fixed_salt_matches_deterministic_expectation():
    salt = bytes(range(8))  # deterministic, test-only — real calls never pass this
    ciphertext = encrypt_openssl(KNOWN_PLAINTEXT, PASSPHRASE, salt=salt)
    # same plaintext+passphrase+salt must always produce the same envelope
    ciphertext2 = encrypt_openssl(KNOWN_PLAINTEXT, PASSPHRASE, salt=salt)
    assert ciphertext == ciphertext2
    assert decrypt_openssl(ciphertext, PASSPHRASE) == KNOWN_PLAINTEXT


def test_two_encryptions_of_same_plaintext_differ_without_fixed_salt():
    c1 = encrypt_openssl(KNOWN_PLAINTEXT, PASSPHRASE)
    c2 = encrypt_openssl(KNOWN_PLAINTEXT, PASSPHRASE)
    assert c1 != c2
