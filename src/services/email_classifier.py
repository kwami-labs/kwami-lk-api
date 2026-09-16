"""Rule-based email classifier and Action Card data extractor.

Assigns a ``kwami_email_category`` to each incoming email and extracts
structured metadata for the front-end Action Card rendering.  The heuristics
here are intentionally simple (sender-domain + subject patterns).  They can be
replaced by an LLM-based pipeline later without changing the DB schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Category enum values (must match the Postgres enum)
# ---------------------------------------------------------------------------
TRAVEL = "travel"
BILLS = "bills"
EVENTS = "events"
NEWSLETTERS = "newsletters"
PERSONAL = "personal"
NOTIFICATIONS = "notifications"
SHOPPING = "shopping"
WORK = "work"
UNCATEGORIZED = "uncategorized"


@dataclass
class ClassificationResult:
    category: str = UNCATEGORIZED
    action_card_data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sender-domain rules
# ---------------------------------------------------------------------------
_DOMAIN_CATEGORY: list[tuple[list[str], str]] = [
    # Travel
    (
        [
            "booking.com",
            "airbnb.com",
            "expedia.com",
            "hotels.com",
            "kayak.com",
            "tripadvisor.com",
            "skyscanner.com",
            "united.com",
            "delta.com",
            "aa.com",
            "southwest.com",
            "ryanair.com",
            "easyjet.com",
            "flydubai.com",
        ],
        TRAVEL,
    ),
    # Bills
    (
        [
            "paypal.com",
            "venmo.com",
            "chase.com",
            "bankofamerica.com",
            "capitalone.com",
            "amex.com",
            "discover.com",
            "citi.com",
            "mint.com",
            "plaid.com",
            "stripe.com",
            "invoicing.",
        ],
        BILLS,
    ),
    # Shopping
    (
        [
            "amazon.com",
            "ebay.com",
            "etsy.com",
            "shopify.com",
            "walmart.com",
            "target.com",
            "bestbuy.com",
            "aliexpress.com",
        ],
        SHOPPING,
    ),
    # Newsletters
    (
        [
            "substack.com",
            "medium.com",
            "mailchimp.com",
            "beehiiv.com",
            "convertkit.com",
            "buttondown.email",
            "revue.email",
        ],
        NEWSLETTERS,
    ),
    # Notifications
    (
        [
            "github.com",
            "gitlab.com",
            "bitbucket.org",
            "vercel.com",
            "netlify.com",
            "sentry.io",
            "linear.app",
            "notion.so",
            "slack.com",
            "discord.com",
            "trello.com",
            "asana.com",
        ],
        NOTIFICATIONS,
    ),
    # Events
    (
        [
            "eventbrite.com",
            "meetup.com",
            "zoom.us",
            "calendly.com",
            "lu.ma",
            "ticketmaster.com",
        ],
        EVENTS,
    ),
]

# ---------------------------------------------------------------------------
# Subject-line keyword patterns
# ---------------------------------------------------------------------------
_SUBJECT_PATTERNS: list[tuple[re.Pattern[str], str, list[str]]] = [
    # Travel
    (
        re.compile(
            r"(flight|boarding pass|itinerary|hotel reservation|trip|booking confirm)",
            re.I,
        ),
        TRAVEL,
        ["flight_info"],
    ),
    # Bills
    (
        re.compile(
            r"(invoice|payment due|bill|statement|receipt|amount due|overdue)",
            re.I,
        ),
        BILLS,
        ["amount", "due_date"],
    ),
    # Events
    (
        re.compile(
            r"(invitation|you.re invited|rsvp|event|calendar|meeting|webinar)",
            re.I,
        ),
        EVENTS,
        ["event_name", "event_date"],
    ),
    # Shopping
    (
        re.compile(
            r"(order confirm|shipped|out for delivery|tracking|delivery|your order)",
            re.I,
        ),
        SHOPPING,
        ["order_status", "tracking_url"],
    ),
    # Newsletters
    (
        re.compile(
            r"(newsletter|digest|weekly|roundup|issue\s*#|unsubscribe)",
            re.I,
        ),
        NEWSLETTERS,
        [],
    ),
    # Work
    (
        re.compile(
            r"(project update|standup|sprint|jira|pull request|code review|deploy)",
            re.I,
        ),
        WORK,
        [],
    ),
]


def _extract_domain(address: str) -> str:
    """Return the bare domain from an email address or empty string."""
    at = address.rfind("@")
    if at == -1:
        return ""
    return address[at + 1 :].lower().strip().rstrip(">")


_AMOUNT_RE = re.compile(r"\$\s?([\d,]+\.?\d{0,2})")
_DATE_RE = re.compile(
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+ \d{1,2},? \d{4})",
)


def _extract_card_data(
    subject: str,
    body: str,
    category: str,
) -> dict[str, Any]:
    """Best-effort structured data extraction for card rendering."""
    data: dict[str, Any] = {}
    combined = f"{subject}\n{body[:2000]}"

    if category == BILLS:
        m = _AMOUNT_RE.search(combined)
        if m:
            data["amount"] = m.group(1)
        m = _DATE_RE.search(combined)
        if m:
            data["due_date"] = m.group(1)

    elif category == TRAVEL:
        m = _DATE_RE.search(combined)
        if m:
            data["travel_date"] = m.group(1)
        data["summary"] = subject[:120]

    elif category == EVENTS:
        m = _DATE_RE.search(combined)
        if m:
            data["event_date"] = m.group(1)
        data["event_name"] = subject[:120]

    elif category == SHOPPING:
        tracking = re.search(r"https?://[^\s]+track[^\s]*", combined, re.I)
        if tracking:
            data["tracking_url"] = tracking.group(0)
        data["summary"] = subject[:120]

    return data


def classify(
    *,
    from_address: str,
    subject: str,
    body_text: str,
) -> ClassificationResult:
    """Classify an email and extract Action Card data."""
    domain = _extract_domain(from_address)

    # 1. Domain lookup
    for domains, cat in _DOMAIN_CATEGORY:
        if any(domain.endswith(d) for d in domains):
            return ClassificationResult(
                category=cat,
                action_card_data=_extract_card_data(subject, body_text, cat),
            )

    # 2. Subject-line patterns
    for pattern, cat, _hints in _SUBJECT_PATTERNS:
        if pattern.search(subject):
            return ClassificationResult(
                category=cat,
                action_card_data=_extract_card_data(subject, body_text, cat),
            )

    # 3. Body-text fallback (only first 2000 chars)
    snippet = body_text[:2000]
    for pattern, cat, _hints in _SUBJECT_PATTERNS:
        if pattern.search(snippet):
            return ClassificationResult(
                category=cat,
                action_card_data=_extract_card_data(subject, body_text, cat),
            )

    # 4. Default: personal if it looks like a real person wrote it
    if len(body_text) > 40 and not re.search(r"(unsubscribe|noreply|no-reply)", from_address, re.I):
        return ClassificationResult(
            category=PERSONAL,
            action_card_data={"summary": subject[:120]},
        )

    return ClassificationResult()
