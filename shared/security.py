import hmac
import json
import re
from datetime import UTC, datetime
from hashlib import sha256


CLIENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
NONCE_RE = re.compile(r"^[a-zA-Z0-9_.:-]{16,128}$")


def canonical_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def signature_payload(timestamp: str, nonce: str, body: str) -> bytes:
    return f"{timestamp}\n{nonce}\n{body}".encode("utf-8")


def sign_body(secret: str, timestamp: str, nonce: str, body: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        signature_payload(timestamp, nonce, body),
        sha256,
    ).hexdigest()


def signature_matches(secret: str, timestamp: str, nonce: str, body: str, signature: str) -> bool:
    expected = sign_body(secret, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)


def parse_utc_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)
