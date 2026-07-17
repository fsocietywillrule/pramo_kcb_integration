import base64
import binascii
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _extract_signature_value(signature_header: str) -> str:
    if not signature_header:
        return ""

    signature_header = signature_header.strip()
    match = re.search(r'signature="?([^",]+)"?', signature_header, flags=re.I)
    if match:
        return match.group(1).strip()
    return signature_header


def _decode_signature(signature_header: str) -> bytes:
    value = _extract_signature_value(signature_header)
    if not value:
        return b""

    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        pass

    try:
        return binascii.unhexlify(value)
    except (binascii.Error, ValueError):
        pass

    return value.encode("utf-8")


def verify_rsa_signature(public_key_pem: str, signature_header: str, payload: bytes) -> tuple[bool, str]:
    if not public_key_pem:
        return False, "Missing public key"
    if not signature_header:
        return False, "Missing signature header"
    if payload is None:
        payload = b""

    signature = _decode_signature(signature_header)
    if not signature:
        return False, "Could not decode signature"

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))

    for hash_algorithm in (hashes.SHA256(), hashes.SHA1()):
        try:
            public_key.verify(signature, payload, padding.PKCS1v15(), hash_algorithm)
            return True, f"Verified with {hash_algorithm.name}"
        except InvalidSignature:
            continue

    return False, "Invalid signature"

