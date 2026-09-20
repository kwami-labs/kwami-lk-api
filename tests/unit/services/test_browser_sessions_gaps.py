"""Two branches `test_browser_contexts.py` does not reach.

Written alongside that file rather than into it, so the two sessions working in
this tree do not collide. Fold these in if it ever makes sense to.
"""

from __future__ import annotations

import pytest

from src.services import browser_sessions
from src.services.browser_sessions import _single, delete_browser_context, save_browser_context


class TestSingle:
    def test_a_list_yields_its_first_row(self):
        assert _single(type("R", (), {"data": [{"id": 1}, {"id": 2}]})()) == {"id": 1}

    def test_an_empty_list_is_none(self):
        assert _single(type("R", (), {"data": []})()) is None

    def test_a_bare_dict_passes_through(self):
        """PostgREST returns an object rather than a list for `.single()`."""
        assert _single(type("R", (), {"data": {"id": 1}})()) == {"id": 1}

    def test_a_result_with_no_data_attribute_is_none(self):
        assert _single(object()) is None


class TestDeleteScopedToAVendor:
    @pytest.mark.anyio
    async def test_naming_a_vendor_only_drops_that_vendors_row(self, fake_supabase):
        await save_browser_context("owner-1", "browserbase", "ctx-bb")
        await save_browser_context("owner-1", "browser_use", "ctx-bu")

        removed = await delete_browser_context("owner-1", "browserbase")

        assert removed == 1
        remaining = [r["vendor"] for r in fake_supabase.db.rows("browser_contexts")]
        assert remaining == ["browser_use"]

    @pytest.mark.anyio
    async def test_omitting_the_vendor_drops_every_row_for_the_owner(self, fake_supabase):
        await save_browser_context("owner-1", "browserbase", "ctx-bb")
        await save_browser_context("owner-1", "browser_use", "ctx-bu")

        assert await delete_browser_context("owner-1", None) == 2
        assert fake_supabase.db.rows("browser_contexts") == []

    @pytest.mark.anyio
    async def test_an_unsupported_vendor_is_refused_before_deleting(self, fake_supabase):
        await save_browser_context("owner-1", "browserbase", "ctx-bb")
        with pytest.raises(browser_sessions.UnsupportedVendorError):
            await delete_browser_context("owner-1", "not-a-vendor")
        assert len(fake_supabase.db.rows("browser_contexts")) == 1

    @pytest.mark.anyio
    async def test_another_owners_rows_are_untouched(self, fake_supabase):
        await save_browser_context("owner-1", "browserbase", "ctx-1")
        await save_browser_context("owner-2", "browserbase", "ctx-2")

        await delete_browser_context("owner-1", "browserbase")

        assert [r["owner_key"] for r in fake_supabase.db.rows("browser_contexts")] == ["owner-2"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("owner_key", ["", "   ", None])
    async def test_a_blank_owner_key_is_refused(self, fake_supabase, owner_key):
        """Deleting with no owner would otherwise match every row in the table."""
        await save_browser_context("owner-1", "browserbase", "ctx-1")
        with pytest.raises(ValueError, match="owner_key is required"):
            await delete_browser_context(owner_key, "browserbase")
        assert len(fake_supabase.db.rows("browser_contexts")) == 1
