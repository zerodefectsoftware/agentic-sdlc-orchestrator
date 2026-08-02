"""codes — minting and validating short codes.

Seven base62 characters: 62**7 is about 3.5e12 codes, so at the register's
10k links/day (A3) the space stays effectively empty for the life of the
service and random minting collides rarely enough that one retry loop in
`links` covers AC1.4 without a coordination service.

Random, not sequential: sequential codes make every link in the service
enumerable, and nothing in the register asks for that to be possible.

This module knows nothing about storage. Uniqueness is a property of the
datastore, so `links` mints a candidate here and checks it there.
"""

from __future__ import annotations

ALPHABET: str = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
CODE_LENGTH: int = 7


def generate_code(length: int = CODE_LENGTH) -> str:
    """Mint one candidate code of `length` characters drawn from `ALPHABET`."""
    raise NotImplementedError


def is_valid_code(code: str) -> bool:
    """Whether `code` matches the documented character set and length."""
    raise NotImplementedError
