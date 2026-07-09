from __future__ import annotations

import base64
import os

import pytest

from core.crypto import (
    DecryptionError,
    UnknownKeyVersionError,
    decrypt_secret,
    encrypt_secret,
)


def _new_kek() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def _set_current_kek(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    *,
    version: int = 1,
) -> None:
    monkeypatch.setenv("OAUTH_KEK", key)
    monkeypatch.setenv("OAUTH_KEK_VERSION", str(version))


def test_encrypt_decrypt_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_current_kek(monkeypatch, _new_kek())

    encrypted = encrypt_secret("oauth-access-token")

    assert encrypted.key_version == 1
    assert decrypt_secret(encrypted.blob, encrypted.key_version) == "oauth-access-token"


@pytest.mark.parametrize("byte_index", [0, 10, 35, -1])
def test_tampered_blob_raises_decryption_error(
    monkeypatch: pytest.MonkeyPatch,
    byte_index: int,
) -> None:
    _set_current_kek(monkeypatch, _new_kek())
    encrypted = encrypt_secret("oauth-access-token")
    tampered = bytearray(encrypted.blob)
    tampered[byte_index] ^= 0x01

    with pytest.raises(DecryptionError):
        decrypt_secret(bytes(tampered), encrypted.key_version)


def test_wrong_kek_raises_decryption_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_current_kek(monkeypatch, _new_kek())
    encrypted = encrypt_secret("oauth-access-token")

    _set_current_kek(monkeypatch, _new_kek())

    with pytest.raises(DecryptionError):
        decrypt_secret(encrypted.blob, encrypted.key_version)


def test_unknown_key_version_raises_unknown_key_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_current_kek(monkeypatch, _new_kek())
    encrypted = encrypt_secret("oauth-access-token")

    with pytest.raises(UnknownKeyVersionError):
        decrypt_secret(encrypted.blob, 99)


def test_same_plaintext_encrypts_to_different_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_current_kek(monkeypatch, _new_kek())

    first = encrypt_secret("same-token")
    second = encrypt_secret("same-token")

    assert first.key_version == second.key_version
    assert first.blob != second.blob


def test_key_rotation_decrypts_old_blobs_and_encrypts_with_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_1_key = _new_kek()
    version_2_key = _new_kek()
    _set_current_kek(monkeypatch, version_1_key, version=1)
    old_encrypted = encrypt_secret("old-refresh-token")

    _set_current_kek(monkeypatch, version_2_key, version=2)
    monkeypatch.setenv("OAUTH_KEK_V1", version_1_key)
    new_encrypted = encrypt_secret("new-refresh-token")

    assert old_encrypted.key_version == 1
    assert new_encrypted.key_version == 2
    assert decrypt_secret(old_encrypted.blob, old_encrypted.key_version) == "old-refresh-token"
    assert decrypt_secret(new_encrypted.blob, new_encrypted.key_version) == "new-refresh-token"
