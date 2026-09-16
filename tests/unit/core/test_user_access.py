"""`check_user_access` decides who can read and delete a memory namespace.

The contract is three anchored rules. The property test is the point: for any two
distinct users, nothing derivable from one user's id may grant access to a
namespace belonging to the other.
"""

import pytest

from src.core.security import AuthUser, check_user_access


def user(user_id: str) -> AuthUser:
    return AuthUser({"sub": user_id, "email": f"{user_id}@example.com"})


@pytest.mark.parametrize(
    "namespace",
    [
        "abc",  # own id
        "kwami_abc",  # legacy shared namespace
        "kwami_abc_k1",  # per-kwami namespace
        "kwami_abc_anything-at-all",
    ],
)
def test_grants_access_to_own_namespaces(namespace: str):
    assert check_user_access(user("abc"), namespace) is True


@pytest.mark.parametrize(
    "namespace",
    [
        "abcd",  # another user whose id merely starts with ours
        "kwami_abcd",
        "kwami_abcd_k1",
        "kwami_victim",
        "victim",
        "",
        # These are the cases the removed `user_id.replace("kwami_", "")` fallback
        # accepted: strings that mangle down to the caller's id without being a
        # namespace the caller owns.
        "abckwami_",
        "kwami_kwami_abc",
        "abkwami_c",
    ],
)
def test_denies_everything_else(namespace: str):
    assert check_user_access(user("abc"), namespace) is False


@pytest.mark.parametrize(
    "victim_namespace",
    ["victim", "kwami_victim", "kwami_victim_k1"],
)
def test_no_derivation_of_one_user_reaches_another(victim_namespace: str):
    """The property that matters: attacker-controlled ids never cross tenants."""
    attacker = user("attacker")
    assert check_user_access(attacker, victim_namespace) is False


def test_empty_subject_never_grants_access():
    """A token with no `sub` must not match an empty namespace into access."""
    assert check_user_access(user(""), "") is False
    assert check_user_access(user(""), "kwami_") is False
