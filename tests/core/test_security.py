import jwt
import pytest

from src.core.config import settings
from src.core.security import (
    AuthUser,
    check_user_access,
    is_admin_user,
    is_valid_admin_api_key,
    verify_token,
)


def test_auth_user_model():
    """Test AuthUser model initialization."""
    payload = {"sub": "123", "email": "test@example.com", "role": "admin"}
    user = AuthUser(payload)
    assert user.id == "123"
    assert user.email == "test@example.com"
    assert user.role == "admin"


def test_check_user_access():
    """Test access control logic."""
    user = AuthUser({"sub": "user123"})

    # Can access own data
    assert check_user_access(user, "user123") is True
    assert check_user_access(user, "kwami_user123") is True

    # Cannot access others
    assert check_user_access(user, "user456") is False
    assert check_user_access(user, "kwami_user456") is False


@pytest.mark.anyio
async def test_verify_token_no_jwks():
    """Test verification fails if JWKS not configured."""
    original_url = settings.supabase_url
    settings.supabase_url = None

    try:
        with pytest.raises(jwt.InvalidTokenError, match="JWKS not configured"):
            await verify_token("fake-token")
    finally:
        settings.supabase_url = original_url


def test_is_admin_user_by_email():
    original = settings.admin_emails_str
    settings.admin_emails_str = "admin@example.com, owner@example.com"

    try:
        assert is_admin_user(AuthUser({"sub": "1", "email": "admin@example.com"})) is True
        assert is_admin_user(AuthUser({"sub": "2", "email": "user@example.com"})) is False
    finally:
        settings.admin_emails_str = original


def test_is_valid_admin_api_key():
    original = settings.admin_api_key
    settings.admin_api_key = "secret-admin-key"

    try:
        assert is_valid_admin_api_key("secret-admin-key") is True
        assert is_valid_admin_api_key("wrong-key") is False
    finally:
        settings.admin_api_key = original
