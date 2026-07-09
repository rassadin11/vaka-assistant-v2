"""Envelope encryption for per-user OAuth secrets.

Key configuration:
- ``OAUTH_KEK`` contains the current 32-byte key-encryption key (KEK),
  encoded with standard base64.
- ``OAUTH_KEK_VERSION`` contains the current integer key version used for new
  encryptions. It defaults to ``1``.
- ``OAUTH_KEK_V<N>`` contains older 32-byte base64 KEKs used only to decrypt
  blobs whose database ``key_version`` is ``N``.

For example, after rotating from version 1 to 2, set ``OAUTH_KEK_VERSION=2``,
put the version-2 key in ``OAUTH_KEK``, and keep the old version-1 key in
``OAUTH_KEK_V1`` until all version-1 rows have been re-encrypted.

Blob format version 1:
- magic: 4 bytes, ``PAES``
- format version: 1 byte
- KEK-wrap nonce: 12 bytes
- payload nonce: 12 bytes
- wrapped data-key length: 2-byte unsigned big-endian integer
- wrapped data key: AES-256-GCM ciphertext plus tag for a fresh 32-byte data key
- payload ciphertext: AES-256-GCM ciphertext plus tag for the UTF-8 plaintext

The database ``key_version`` column remains the source of truth for KEK lookup
and is authenticated as associated data during both the data-key unwrap and the
payload decrypt operations.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"PAES"
_FORMAT_VERSION = 1
_KEK_BYTES = 32
_DATA_KEY_BYTES = 32
_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16
_WRAPPED_DATA_KEY_BYTES = _DATA_KEY_BYTES + _GCM_TAG_BYTES
_LENGTH_FIELD_BYTES = 2
_HEADER_BYTES = len(_MAGIC) + 1 + _NONCE_BYTES + _NONCE_BYTES + _LENGTH_FIELD_BYTES
_WRAP_AAD_PREFIX = b"personal-assistant.oauth-keywrap.v1"
_PAYLOAD_AAD_PREFIX = b"personal-assistant.oauth-secret.v1"


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """Encrypted secret bytes plus the KEK version needed to decrypt them."""

    blob: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class _ParsedBlob:
    header: bytes
    wrap_nonce: bytes
    payload_nonce: bytes
    wrapped_data_key: bytes
    payload_ciphertext: bytes


class CryptoConfigurationError(Exception):
    """Base class for encryption key configuration errors."""


class MissingKeyError(CryptoConfigurationError):
    """Raised when the current KEK required for encryption is not configured."""


class UnknownKeyVersionError(CryptoConfigurationError):
    """Raised when no KEK is configured for a requested key version."""


class DecryptionError(Exception):
    """Raised when an encrypted secret cannot be authenticated or decoded."""


def encrypt_secret(plaintext: str) -> EncryptedSecret:
    """Encrypt a UTF-8 string with a fresh data key and the current KEK."""

    key_version = _current_key_version()
    kek = _current_kek()
    data_key = os.urandom(_DATA_KEY_BYTES)
    wrap_nonce = os.urandom(_NONCE_BYTES)
    payload_nonce = os.urandom(_NONCE_BYTES)

    wrapped_data_key = AESGCM(kek).encrypt(wrap_nonce, data_key, _wrap_aad(key_version))
    header = _build_header(wrap_nonce, payload_nonce, len(wrapped_data_key))
    payload_ciphertext = AESGCM(data_key).encrypt(
        payload_nonce,
        plaintext.encode("utf-8"),
        _payload_aad(key_version, header, wrapped_data_key),
    )

    return EncryptedSecret(
        blob=header + wrapped_data_key + payload_ciphertext,
        key_version=key_version,
    )


def decrypt_secret(blob: bytes, key_version: int) -> str:
    """Decrypt an encrypted secret blob with the configured KEK version."""

    kek = _kek_for_version(key_version)
    parsed = _parse_blob(blob)

    try:
        data_key = AESGCM(kek).decrypt(
            parsed.wrap_nonce,
            parsed.wrapped_data_key,
            _wrap_aad(key_version),
        )
        if len(data_key) != _DATA_KEY_BYTES:
            raise DecryptionError("Encrypted secret is invalid.")
        plaintext = AESGCM(data_key).decrypt(
            parsed.payload_nonce,
            parsed.payload_ciphertext,
            _payload_aad(key_version, parsed.header, parsed.wrapped_data_key),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise DecryptionError("Encrypted secret could not be decrypted.") from exc


def generate_base64_kek() -> str:
    """Return a fresh base64-encoded 32-byte KEK for local setup or rotation."""

    return base64.b64encode(os.urandom(_KEK_BYTES)).decode("ascii")


def _current_key_version() -> int:
    raw_version = os.getenv("OAUTH_KEK_VERSION", "1")
    try:
        key_version = int(raw_version)
    except ValueError as exc:
        raise MissingKeyError("OAUTH_KEK_VERSION must be a positive integer.") from exc

    if key_version < 1:
        raise MissingKeyError("OAUTH_KEK_VERSION must be a positive integer.")
    return key_version


def _current_kek() -> bytes:
    raw_key = os.getenv("OAUTH_KEK")
    if raw_key is None:
        raise MissingKeyError("OAUTH_KEK is required for OAuth secret encryption.")
    return _decode_kek(raw_key, env_name="OAUTH_KEK", error_type=MissingKeyError)


def _kek_for_version(key_version: int) -> bytes:
    if key_version < 1:
        raise UnknownKeyVersionError("No KEK is configured for the requested key version.")

    current_version = _current_key_version()
    if key_version == current_version:
        return _current_kek()

    env_name = f"OAUTH_KEK_V{key_version}"
    raw_key = os.getenv(env_name)
    if raw_key is None:
        raise UnknownKeyVersionError("No KEK is configured for the requested key version.")
    return _decode_kek(raw_key, env_name=env_name, error_type=UnknownKeyVersionError)


def _decode_kek(
    raw_key: str,
    *,
    env_name: str,
    error_type: type[CryptoConfigurationError],
) -> bytes:
    try:
        key = base64.b64decode(raw_key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise error_type(f"{env_name} must be standard base64.") from exc

    if len(key) != _KEK_BYTES:
        raise error_type(f"{env_name} must decode to 32 bytes.")
    return key


def _build_header(wrap_nonce: bytes, payload_nonce: bytes, wrapped_key_len: int) -> bytes:
    return (
        _MAGIC
        + bytes([_FORMAT_VERSION])
        + wrap_nonce
        + payload_nonce
        + wrapped_key_len.to_bytes(_LENGTH_FIELD_BYTES, byteorder="big")
    )


def _parse_blob(blob: bytes) -> _ParsedBlob:
    minimum_len = _HEADER_BYTES + _WRAPPED_DATA_KEY_BYTES + _GCM_TAG_BYTES
    if len(blob) < minimum_len:
        raise DecryptionError("Encrypted secret blob is invalid.")
    if blob[: len(_MAGIC)] != _MAGIC:
        raise DecryptionError("Encrypted secret blob is invalid.")
    if blob[len(_MAGIC)] != _FORMAT_VERSION:
        raise DecryptionError("Encrypted secret blob version is unsupported.")

    wrap_nonce_start = len(_MAGIC) + 1
    payload_nonce_start = wrap_nonce_start + _NONCE_BYTES
    length_start = payload_nonce_start + _NONCE_BYTES
    wrapped_key_start = length_start + _LENGTH_FIELD_BYTES

    wrapped_key_len = int.from_bytes(
        blob[length_start:wrapped_key_start],
        byteorder="big",
    )
    if wrapped_key_len != _WRAPPED_DATA_KEY_BYTES:
        raise DecryptionError("Encrypted secret blob is invalid.")

    payload_start = wrapped_key_start + wrapped_key_len
    if len(blob) < payload_start + _GCM_TAG_BYTES:
        raise DecryptionError("Encrypted secret blob is invalid.")

    return _ParsedBlob(
        header=blob[:wrapped_key_start],
        wrap_nonce=blob[wrap_nonce_start:payload_nonce_start],
        payload_nonce=blob[payload_nonce_start:length_start],
        wrapped_data_key=blob[wrapped_key_start:payload_start],
        payload_ciphertext=blob[payload_start:],
    )


def _version_bytes(key_version: int) -> bytes:
    return key_version.to_bytes(4, byteorder="big", signed=False)


def _wrap_aad(key_version: int) -> bytes:
    return _WRAP_AAD_PREFIX + _version_bytes(key_version)


def _payload_aad(key_version: int, header: bytes, wrapped_data_key: bytes) -> bytes:
    return _PAYLOAD_AAD_PREFIX + _version_bytes(key_version) + header + wrapped_data_key
