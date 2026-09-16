"""Cryptographic core.

Design summary
--------------
::

    MASTER_KEK (env only, 32 bytes)
       |
       +-- wraps -> users.dek_wrapped            per-user Data Encryption Key
       |              |
       |              +-- HKDF-SHA256(salt=node_id, info="teledrive/file/v1")
       |                     |
       |                     +-> per-file key -> AES-256-GCM, unique IV/chunk
       |
       +-- encrypts -> telegram_sessions.session_enc   StringSession at rest

Two properties matter and are enforced here rather than left to callers:

1. **A unique IV per chunk is mandatory.** Reusing an IV under the same key
   destroys GCM confidentiality *and* authenticity. IVs are generated with
   ``secrets.token_bytes(12)`` from the system CSPRNG and are never derived from
   a counter that could restart.

2. **Chunk position is authenticated.** The GCM associated data binds
   ``node_id || chunk_index || total_chunks || crypto_version``, so a reordered,
   swapped, or truncated chunk fails authentication instead of decrypting into
   plausible-looking garbage. Without this, an attacker who controls the message
   ordering could reassemble a file from chunks of another file.

The mode is deliberately split: ``server_managed`` derives keys server-side so
previews are possible, while ``zero_knowledge`` accepts a client-supplied IV and
key, in which case the server can never read the plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import Settings

AES_KEY_BYTES = 32  # AES-256
GCM_IV_BYTES = 12   # 96-bit nonce, the GCM-recommended size
GCM_TAG_BYTES = 16

CRYPTO_VERSION = 1
AAD_VERSION = 1

FILE_KEY_INFO = b"teledrive/file/v1"
DEK_WRAP_INFO = b"teledrive/dek-wrap/v1"
SESSION_INFO = b"teledrive/session/v1"
THUMBNAIL_INFO = b"teledrive/thumbnail/v1"


class CryptoError(RuntimeError):
    """Raised when a cryptographic operation cannot proceed safely."""


class AuthenticationFailed(CryptoError):
    """GCM tag verification failed: the ciphertext or its context was tampered with."""


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
#
# scrypt is used from the standard library so that the core has no hard
# dependency on a native Argon2 build, which is a common installation failure on
# new Python releases. Argon2id is preferred when available and is selected
# automatically, so existing hashes keep verifying either way. The stored format
# is a self-describing PHC-like string, which makes algorithm migration a
# non-breaking, per-user operation.

_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2

try:  # pragma: no cover - depends on the environment
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import VerificationError as _Argon2VerificationError
    from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch

    _ARGON2: _Argon2Hasher | None = _Argon2Hasher(
        time_cost=3, memory_cost=64 * 1024, parallelism=4
    )
except Exception:  # noqa: BLE001 - optional dependency
    _ARGON2 = None
    _Argon2VerificationError = Exception  # type: ignore[assignment,misc]
    _Argon2Mismatch = Exception  # type: ignore[assignment,misc]


def hash_password(password: str) -> str:
    """Hash a password for storage.

    Argon2id when the library is present, otherwise scrypt. Both are memory-hard
    and both are recorded in the output string, so verification never has to
    guess.
    """
    if _ARGON2 is not None:
        return _ARGON2.hash(password)

    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password verification."""
    if not stored:
        return False

    if stored.startswith("$argon2"):
        if _ARGON2 is None:
            raise CryptoError("stored hash is Argon2id but the argon2 library is unavailable")
        try:
            return _ARGON2.verify(stored, password)
        except (_Argon2Mismatch, _Argon2VerificationError):
            return False
        except Exception:  # noqa: BLE001
            return False

    if stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt_b64, digest_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
                maxmem=128 * int(n) * int(r) * 2,
            )
        except Exception:  # noqa: BLE001 - malformed hash
            return False
        return hmac.compare_digest(expected, actual)

    return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses weaker parameters than the current policy."""
    if stored.startswith("$argon2"):
        if _ARGON2 is None:
            return False
        try:
            return _ARGON2.check_needs_rehash(stored)
        except Exception:  # noqa: BLE001
            return False
    # Anything not Argon2id should be upgraded once the library is available.
    return _ARGON2 is not None or stored.startswith("scrypt$")


# ---------------------------------------------------------------------------
# Key derivation and wrapping
# ---------------------------------------------------------------------------

def derive_subkey(master: bytes, *, salt: bytes, info: bytes, length: int = AES_KEY_BYTES) -> bytes:
    """HKDF-SHA256 expansion.

    HKDF is the right primitive here rather than a bare hash: it is
    extract-then-expand, so it produces independent keys from one master secret
    even when the salt is reused, and it is domain-separated by ``info``.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    ).derive(master)


def generate_dek() -> bytes:
    """Generate a fresh per-user Data Encryption Key."""
    return secrets.token_bytes(AES_KEY_BYTES)


def wrap_dek(dek: bytes, kek: bytes, *, user_id_bytes: bytes) -> bytes:
    """Wrap a DEK with the MASTER_KEK for storage.

    The user id is bound as associated data, so a wrapped key cannot be moved
    between users by editing a row: decryption fails loudly instead of silently
    handing one user another's key.
    """
    if len(kek) != AES_KEY_BYTES:
        raise CryptoError("MASTER_KEK must be 32 bytes")
    nonce = secrets.token_bytes(GCM_IV_BYTES)
    aad = DEK_WRAP_INFO + user_id_bytes
    ct = AESGCM(kek).encrypt(nonce, dek, aad)
    return nonce + ct  # 12-byte nonce prefix, then ciphertext||tag


def unwrap_dek(wrapped: bytes, kek: bytes, *, user_id_bytes: bytes) -> bytes:
    """Reverse :func:`wrap_dek`."""
    if len(wrapped) < GCM_IV_BYTES + GCM_TAG_BYTES:
        raise CryptoError("wrapped DEK is too short to be valid")
    nonce, ct = wrapped[:GCM_IV_BYTES], wrapped[GCM_IV_BYTES:]
    try:
        return AESGCM(kek).decrypt(nonce, ct, DEK_WRAP_INFO + user_id_bytes)
    except InvalidTag as exc:
        raise AuthenticationFailed(
            "wrapped DEK failed authentication: wrong MASTER_KEK or tampered row"
        ) from exc


def derive_file_key(dek: bytes, *, node_id_bytes: bytes, key_version: int = 1) -> bytes:
    """Derive a per-file key from a user DEK.

    Salting with the node id means no per-file key material has to be stored,
    while files remain cryptographically isolated: compromising one file's key
    reveals nothing about any other file.
    """
    return derive_subkey(
        dek,
        salt=node_id_bytes,
        info=FILE_KEY_INFO + struct.pack(">I", key_version),
    )


# ---------------------------------------------------------------------------
# Chunk AEAD
# ---------------------------------------------------------------------------

def build_chunk_aad(
    node_id_bytes: bytes,
    chunk_index: int,
    total_chunks: int,
    crypto_version: int = CRYPTO_VERSION,
) -> bytes:
    """Associated data binding a chunk to its exact position in one file.

    This is the mechanism that makes chunk reordering and cross-file substitution
    detectable rather than silently accepted.
    """
    return b"".join(
        (
            struct.pack(">I", AAD_VERSION),
            node_id_bytes,
            struct.pack(">Q", chunk_index),
            struct.pack(">Q", total_chunks),
            struct.pack(">H", crypto_version),
        )
    )


def new_iv() -> bytes:
    """A fresh 96-bit IV from the system CSPRNG.

    Never a counter: a restarted process or a restored backup could repeat it,
    and GCM nonce reuse is catastrophic.
    """
    return secrets.token_bytes(GCM_IV_BYTES)


@dataclass(frozen=True, slots=True)
class SealedChunk:
    """A chunk after AEAD sealing, with the tag kept separate for the schema."""

    ciphertext: bytes
    iv: bytes
    tag: bytes

    @property
    def wire(self) -> bytes:
        """The exact bytes stored on Telegram: ciphertext followed by the tag."""
        return self.ciphertext + self.tag

    @property
    def wire_size(self) -> int:
        return len(self.ciphertext) + len(self.tag)


def sealed_from_wire(wire: bytes, iv: bytes) -> SealedChunk:
    """Reconstruct a :class:`SealedChunk` from stored bytes plus the indexed IV.

    The IV is deliberately *not* part of the stored blob: it lives in
    ``file_chunks.iv`` because it is needed for decryption and is not secret.
    Keeping it out of the payload means the stored object is exactly
    ``ciphertext || tag``, which makes the expected stored size a simple,
    checkable relation to the plaintext size.
    """
    ciphertext, tag = split_wire(wire)
    return SealedChunk(ciphertext=ciphertext, iv=iv, tag=tag)


def seal_chunk(key: bytes, plaintext: bytes, *, aad: bytes, iv: bytes | None = None) -> SealedChunk:
    """Encrypt one chunk with AES-256-GCM.

    One-shot rather than incremental: a chunk is bounded by the configured chunk
    size (<= 2 GiB) and is already buffered in RAM by design, so an incremental
    API would add complexity without changing the memory profile.
    """
    if len(key) != AES_KEY_BYTES:
        raise CryptoError("chunk key must be 32 bytes")
    iv = iv or new_iv()
    if len(iv) != GCM_IV_BYTES:
        raise CryptoError(f"IV must be {GCM_IV_BYTES} bytes, got {len(iv)}")

    encryptor = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    encryptor.authenticate_additional_data(aad)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return SealedChunk(ciphertext=ciphertext, iv=iv, tag=encryptor.tag)


def open_chunk(
    key: bytes,
    ciphertext: bytes,
    *,
    iv: bytes,
    tag: bytes,
    aad: bytes,
) -> bytes:
    """Decrypt and authenticate one chunk.

    Raises :class:`AuthenticationFailed` on any tampering, including a chunk
    presented at the wrong index, which is the common failure this design exists
    to catch.
    """
    if len(key) != AES_KEY_BYTES:
        raise CryptoError("chunk key must be 32 bytes")
    if len(iv) != GCM_IV_BYTES:
        raise CryptoError(f"IV must be {GCM_IV_BYTES} bytes, got {len(iv)}")
    if len(tag) != GCM_TAG_BYTES:
        raise CryptoError(f"GCM tag must be {GCM_TAG_BYTES} bytes, got {len(tag)}")

    decryptor = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
    decryptor.authenticate_additional_data(aad)
    try:
        return decryptor.update(ciphertext) + decryptor.finalize()
    except InvalidTag as exc:
        raise AuthenticationFailed(
            "chunk failed GCM authentication: wrong key, tampered ciphertext, or wrong position"
        ) from exc


def split_wire(wire: bytes) -> tuple[bytes, bytes]:
    """Split stored bytes into ``(ciphertext, tag)``."""
    if len(wire) < GCM_TAG_BYTES:
        raise CryptoError("stored chunk is shorter than a GCM tag")
    return wire[:-GCM_TAG_BYTES], wire[-GCM_TAG_BYTES:]


# ---------------------------------------------------------------------------
# Content digests
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


class ChunkHasher:
    """Incremental SHA-256 for streaming a whole file without buffering it.

    Chunks are fed in as they are processed, so the whole-file digest is a free
    by-product of an upload rather than a second read pass over the data.
    """

    __slots__ = ("_h", "total_bytes")

    def __init__(self) -> None:
        self._h = hashlib.sha256()
        self.total_bytes = 0

    def update(self, data: bytes) -> None:
        self._h.update(data)
        self.total_bytes += len(data)

    def hexdigest(self) -> str:
        return self._h.hexdigest()

    def digest(self) -> bytes:
        return self._h.digest()


def keyed_content_hmac(key: bytes, data: bytes) -> bytes:
    """Keyed digest used instead of a plaintext SHA-256 in zero-knowledge mode.

    A plaintext hash in ZK mode is a confirmation oracle: an attacker holding
    the ciphertext can verify a guess about the content. A client-held key makes
    the digest comparable for integrity while revealing nothing.
    """
    return hmac.new(key, data, hashlib.sha256).digest()


def constant_time_equals(a: str | bytes, b: str | bytes) -> bool:
    """Comparison for every secret-adjacent check (tokens, HMACs, initData)."""
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# MTProto session protection
# ---------------------------------------------------------------------------

def encrypt_session_string(session_string: str, kek: bytes, *, label: str) -> bytes:
    """Encrypt a Telethon StringSession for storage.

    A session string is equivalent to a logged-in device, so it is treated as a
    bearer credential of the highest sensitivity: AEAD-encrypted under the KEK,
    bound to its label, and never logged or returned by any endpoint.
    """
    nonce = secrets.token_bytes(GCM_IV_BYTES)
    aad = SESSION_INFO + label.encode("utf-8")
    ct = AESGCM(kek).encrypt(nonce, session_string.encode("utf-8"), aad)
    return nonce + ct


def decrypt_session_string(blob: bytes, kek: bytes, *, label: str) -> str:
    nonce, ct = blob[:GCM_IV_BYTES], blob[GCM_IV_BYTES:]
    aad = SESSION_INFO + label.encode("utf-8")
    try:
        return AESGCM(kek).decrypt(nonce, ct, aad).decode("utf-8")
    except InvalidTag as exc:
        raise AuthenticationFailed(
            f"session {label!r} failed authentication: wrong MASTER_KEK or wrong label"
        ) from exc


def session_fingerprint(session_string: str) -> bytes:
    """Digest of a session string, for change detection without decryption."""
    return hashlib.sha256(session_string.encode("utf-8")).digest()


def derive_thumbnail_key(dek: bytes, *, node_id_bytes: bytes) -> bytes:
    """Separate key domain for derived previews.

    Thumbnails must not be encrypted under the file key: reusing one key across
    different plaintext lengths and structures widens the attack surface for no
    benefit, and it would let a thumbnail overwrite be mistaken for file data.
    """
    return derive_subkey(dek, salt=node_id_bytes, info=THUMBNAIL_INFO)


def kek_from_settings(settings: Settings) -> bytes:
    if len(settings.master_kek) != AES_KEY_BYTES:
        raise CryptoError("MASTER_KEK must be 32 bytes; refusing to operate")
    return settings.master_kek