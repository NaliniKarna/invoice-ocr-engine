"""Opaque, non-sequential identifiers. Process IDs: 20 random lowercase alphanumeric characters.
All other kinds: prefixed (122 random bits from uuid4).
UUIDv7 is deliberately not used: it embeds a timestamp, leaking creation time."""
import re
import secrets
import uuid

PREFIXES = {
    "process": "proc",
    "batch": "batch",
    "client": "cli",
    "api_key": "key",
    "feedback": "fb",
    "submission": "sub",
    "review": "rev",
    "result": "res",
    "model": "mdl",
    "audit": "aud",
    "dataset": "ds",
    "idem": "idem",
    "example": "ex",
    "dsex": "dsx",
    "eval": "evr",
}


# Process IDs are what clients see and poll with. They are exactly 20 random characters from a
# lowercase alphanumeric alphabet (36**20, about 103 bits), generated with the OS CSPRNG, no prefix.
PROCESS_ID_LENGTH = 20
PROCESS_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_id(kind: str) -> str:
    if kind == "process":
        return "".join(secrets.choice(PROCESS_ID_ALPHABET) for _ in range(PROCESS_ID_LENGTH))
    return f"{PREFIXES[kind]}_{uuid.uuid4().hex}"


def id_pattern(kind: str) -> str:
    if kind == "process":
        return rf"^[a-z0-9]{{{PROCESS_ID_LENGTH}}}$"
    return rf"^{PREFIXES[kind]}_[0-9a-f]{{32}}$"


def is_valid_id(kind: str, value: str) -> bool:
    return re.fullmatch(id_pattern(kind)[1:-1], value) is not None
