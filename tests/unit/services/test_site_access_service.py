"""`src.services.site_access_service`: the sites Jarvis may visit, and how far."""

from __future__ import annotations

import pytest

from src.services import site_access_service as s

pytestmark = pytest.mark.anyio

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _rows(fake_supabase):
    return fake_supabase.db.rows(s.TABLE)


def _seed(fake_supabase, pattern, access, status="active", user=USER, reason=None):
    return fake_supabase.db.seed(
        s.TABLE,
        {
            "user_id": user,
            "pattern": pattern,
            "access": access,
            "status": status,
            "reason": reason,
        },
    )[0]


class TestSitePattern:
    @pytest.mark.parametrize(
        ("address", "pattern"),
        [
            ("https://www.GitHub.com/octane/repo?x=1", "github.com"),
            ("mail.google.com", "mail.google.com"),
            ("*.amazon.es", "amazon.es"),
            ("http://shop.example.co.uk:8443/cart", "shop.example.co.uk"),
            ("  example.org.  ", "example.org"),
            ("https://bücher.de", "xn--bcher-kva.de"),
            ("203.0.113.9", "203.0.113.9"),
        ],
    )
    def test_an_address_becomes_its_host(self, address, pattern):
        assert s.site_pattern(address) == pattern

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "localhost", "no spaces.com x", "-bad-.com", "a" * 3000, "http://[::1", "a..b"],
    )
    def test_what_is_not_a_host_is_refused(self, bad):
        with pytest.raises(s.InvalidSiteError):
            s.site_pattern(bad)

    def test_a_name_idna_cannot_encode_is_refused(self):
        with pytest.raises(s.InvalidSiteError):
            s.site_pattern("https://" + "é" * 70 + ".com")


class TestLevelFor:
    RULES = {"google.com": "read", "mail.google.com": "account", "evil.com": "blocked"}

    def test_the_longest_pattern_decides(self):
        assert s.level_for("mail.google.com", self.RULES) == "account"
        assert s.level_for("drive.google.com", self.RULES) == "read"
        assert s.level_for("GOOGLE.com.", self.RULES) == "read"

    def test_a_suffix_that_is_not_a_parent_does_not_match(self):
        assert s.level_for("notevil.com", self.RULES, "browse") == "browse"

    def test_without_a_rule_the_default_applies(self):
        assert s.level_for("wikipedia.org", self.RULES) == s.DEFAULT_LEVEL


class TestListAccess:
    async def test_an_empty_list_lets_every_site_through(self, fake_supabase):
        body = await s.list_access(USER)
        assert body == {
            "default": "account",
            "sites": [],
            "pending": [],
            "levels": list(s.LEVELS),
        }

    async def test_sites_requests_and_default_are_kept_apart(self, fake_supabase):
        _seed(fake_supabase, "*", "read")
        _seed(fake_supabase, "github.com", "write")
        _seed(fake_supabase, "amazon.es", "browse", status="pending", reason="compare prices")
        _seed(fake_supabase, "other.com", "account", user=OTHER)

        body = await s.list_access(USER)

        assert body["default"] == "read"
        assert [x["pattern"] for x in body["sites"]] == ["github.com"]
        assert body["pending"][0]["reason"] == "compare prices"
        assert body["pending"][0]["access"] == "browse"

    async def test_a_pending_default_is_not_the_default(self, fake_supabase):
        _seed(fake_supabase, "*", "blocked", status="pending")
        assert (await s.list_access(USER))["default"] == "account"


class TestCheck:
    async def test_it_names_the_rule_that_decides(self, fake_supabase):
        _seed(fake_supabase, "*", "read")
        _seed(fake_supabase, "google.com", "browse")
        _seed(fake_supabase, "mail.google.com", "account")
        _seed(fake_supabase, "bank.com", "write", status="pending")

        assert await s.check(USER, "https://mail.google.com/u/0") == {
            "host": "mail.google.com",
            "access": "account",
            "pattern": "mail.google.com",
        }
        assert (await s.check(USER, "maps.google.com"))["pattern"] == "google.com"
        assert await s.check(USER, "bank.com") == {
            "host": "bank.com",
            "access": "read",
            "pattern": "*",
        }

    async def test_a_bad_address_is_refused(self, fake_supabase):
        with pytest.raises(s.InvalidSiteError):
            await s.check(USER, "localhost")


class TestSetSite:
    async def test_it_is_kept_by_host_and_replaced_by_the_next(self, fake_supabase):
        first = await s.set_site(USER, "https://www.github.com/x", "read")
        await s.set_site(USER, "github.com", "write")

        rows = _rows(fake_supabase)
        assert [(r["pattern"], r["access"], r["status"]) for r in rows] == [
            ("github.com", "write", "active")
        ]
        assert first["pattern"] == "github.com" and first["id"]

    async def test_setting_a_requested_site_answers_the_request(self, fake_supabase):
        _seed(fake_supabase, "amazon.es", "browse", status="pending", reason="prices")
        await s.set_site(USER, "amazon.es", "read")
        row = _rows(fake_supabase)[0]
        assert (row["status"], row["access"], row["reason"]) == ("active", "read", None)

    async def test_an_unknown_level_is_refused(self, fake_supabase):
        with pytest.raises(s.UnknownLevelError):
            await s.set_site(USER, "github.com", "admin")
        assert _rows(fake_supabase) == []

    async def test_the_list_has_an_end(self, fake_supabase, monkeypatch):
        monkeypatch.setattr(s, "MAX_RULES", 2)
        _seed(fake_supabase, "*", "read")
        await s.set_site(USER, "a.com", "read")
        await s.set_site(USER, "b.com", "read")
        await s.set_site(USER, "b.com", "write")
        with pytest.raises(s.TooManySitesError):
            await s.set_site(USER, "c.com", "read")


class TestDefault:
    async def test_it_is_the_row_for_every_other_site(self, fake_supabase):
        assert await s.set_default(USER, "read") == "read"
        assert await s.set_default(USER, "blocked") == "blocked"
        assert [(r["pattern"], r["access"]) for r in _rows(fake_supabase)] == [("*", "blocked")]

    async def test_an_unknown_level_is_refused(self, fake_supabase):
        with pytest.raises(s.UnknownLevelError):
            await s.set_default(USER, "everything")


class TestRemoveSite:
    async def test_only_the_owners_site_goes(self, fake_supabase):
        mine = _seed(fake_supabase, "github.com", "write")
        theirs = _seed(fake_supabase, "github.com", "write", user=OTHER)

        assert await s.remove_site(OTHER, str(mine["id"])) is False
        assert await s.remove_site(USER, str(mine["id"])) is True
        assert [r["id"] for r in _rows(fake_supabase)] == [theirs["id"]]

    async def test_the_default_is_never_removed_here(self, fake_supabase):
        row = _seed(fake_supabase, "*", "read")
        assert await s.remove_site(USER, str(row["id"])) is False
        assert len(_rows(fake_supabase)) == 1


class TestAnswerRequest:
    async def test_approving_keeps_the_level_asked_for(self, fake_supabase):
        row = _seed(fake_supabase, "amazon.es", "browse", status="pending")
        kept = await s.answer_request(USER, str(row["id"]), approve=True)
        assert kept is not None and kept["access"] == "browse"
        assert _rows(fake_supabase)[0]["status"] == "active"

    async def test_approving_at_another_level(self, fake_supabase):
        row = _seed(fake_supabase, "amazon.es", "account", status="pending")
        kept = await s.answer_request(USER, str(row["id"]), approve=True, access="read")
        assert kept is not None and kept["access"] == "read"

    async def test_refusing_forgets_the_request(self, fake_supabase):
        row = _seed(fake_supabase, "amazon.es", "browse", status="pending")
        assert await s.answer_request(USER, str(row["id"]), approve=False) is None
        assert _rows(fake_supabase) == []

    async def test_a_site_already_kept_is_not_a_request(self, fake_supabase):
        row = _seed(fake_supabase, "github.com", "read")
        assert await s.answer_request(USER, str(row["id"]), approve=True, access="account") is None
        assert await s.answer_request(USER, str(row["id"]), approve=False) is None
        assert _rows(fake_supabase)[0]["access"] == "read"

    async def test_someone_else_cannot_answer(self, fake_supabase):
        row = _seed(fake_supabase, "amazon.es", "browse", status="pending")
        assert await s.answer_request(OTHER, str(row["id"]), approve=True) is None
        assert _rows(fake_supabase)[0]["status"] == "pending"

    async def test_an_unknown_level_is_refused(self, fake_supabase):
        with pytest.raises(s.UnknownLevelError):
            await s.answer_request(USER, "x", approve=True, access="root")
