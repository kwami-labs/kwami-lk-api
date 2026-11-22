"""What a call carries from Twilio into LiveKit, and the proof that it came from here.

Twilio dials the LiveKit SIP address with the kwami, its owner and the caller as
SIP headers; LiveKit turns them into the caller's participant attributes. Anybody
who knows the SIP address could send the same headers, so they are signed: the
agent trusts the kwami and, above all, that the caller is the owner (who may then
ask for work on the PC or in the cloud) only when the signature holds and is fresh.

The key is derived from the Supabase secret key, which the API and the agent both
have, so no new secret is needed on either side.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlparse, urlunparse

from src.core.config import settings

SIGNATURE_DOMAIN = "sip-call"


def _key() -> bytes | None:
    secret = settings.supabase_secret_key
    return f"{SIGNATURE_DOMAIN}:{secret}".encode() if secret else None


def sign_call(kwami_id: str, user_id: str, owner: bool, timestamp: int) -> str:
    """The signature over what the agent will trust; empty when there is no key."""
    key = _key()
    if key is None:
        return ""
    message = f"{kwami_id}|{user_id}|{'1' if owner else '0'}|{timestamp}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def call_headers(
    *, kwami_id: str, channel_id: str, user_id: str, caller: str | None, owner: bool
) -> dict[str, str]:
    """The SIP headers of a bridged call, signed."""
    timestamp = int(time.time())
    return {
        "X-Kwami-Id": kwami_id,
        "X-Kwami-Channel-Id": channel_id,
        "X-Kwami-User-Id": user_id,
        "X-Contact-Number": caller or "",
        "X-Kwami-Owner": "1" if owner else "0",
        "X-Kwami-Ts": str(timestamp),
        "X-Kwami-Sig": sign_call(kwami_id, user_id, owner, timestamp),
    }


def sip_uri_for(uri: str, called: str) -> str:
    """The LiveKit SIP address with the dialled number as its user part.

    LiveKit's inbound trunk accepts only the numbers it lists, and reads the number
    from there; an address that already names a user is left as it is.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "sip" or "@" in parsed.path:
        return uri
    return urlunparse(parsed._replace(path=f"{called}@{parsed.path}"))
