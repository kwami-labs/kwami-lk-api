"""Who takes a job: the PC, the cloud, or nobody yet (migration 029).

These rules live in two SQL functions, `brain_claim_job` for the PC and
`brain_claim_cloud_job` for the cloud, and nothing in Python can prove them: the
patience, the user's consent, the cap per user and SKIP LOCKED only hold in
Postgres. So they are pinned here, against the real migrations.
"""

from __future__ import annotations

import concurrent.futures
import uuid

import pytest

pytestmark = pytest.mark.integration

PATIENCE = 60


def _user(db) -> tuple[str, str]:
    user_id, kwami_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email) VALUES (%s, %s)",
            (user_id, f"{user_id}@example.com"),
        )
        cur.execute("INSERT INTO user_kwamis (id, user_id) VALUES (%s, %s)", (kwami_id, user_id))
        cur.execute("TRUNCATE brain_cloud_runners")
    return user_id, kwami_id


def _allow_cloud(db, user_id: str, enabled: bool = True) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO brain_cloud_settings (user_id, enabled) VALUES (%s, %s)",
            (user_id, enabled),
        )


def _job(
    db,
    user_id: str,
    kwami_id: str,
    *,
    target: str = "auto",
    mode: str = "work",
    age_seconds: int = PATIENCE + 5,
    status: str = "queued",
    runner_id: str = "",
) -> str:
    job_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO brain_jobs
                (id, user_id, kwami_id, title, task, target, mode, status, runner_id, created_at)
            VALUES (%s, %s, %s, 't', 'do it', %s, %s, %s, %s,
                    now() - make_interval(secs => %s))
            """,
            (job_id, user_id, kwami_id, target, mode, status, runner_id, age_seconds),
        )
    return job_id


def _pc_seen(db, user_id: str, seconds_ago: int = 0) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO brain_runners (user_id, runner_id, last_seen_at)
            VALUES (%s, 'pc-1', now() - make_interval(secs => %s))
            """,
            (user_id, seconds_ago),
        )


def _cloud_claim(db, runner: str = "cloud-a", per_user: int = 2) -> str | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM brain_claim_cloud_job(%s, %s, %s)", (runner, PATIENCE, per_user)
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _pc_claim(db, user_id: str, thinking: bool = False) -> str | None:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM brain_claim_job(%s, 'pc-1', %s)", (user_id, thinking))
        row = cur.fetchone()
    return str(row[0]) if row else None


def test_the_cloud_takes_nothing_from_a_user_who_did_not_allow_it(db):
    user_id, kwami_id = _user(db)
    _job(db, user_id, kwami_id, target="cloud")
    assert _cloud_claim(db) is None

    _allow_cloud(db, user_id, enabled=False)
    assert _cloud_claim(db) is None


def test_the_cloud_takes_what_was_asked_of_it_even_with_the_pc_on(db):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _pc_seen(db, user_id)
    job_id = _job(db, user_id, kwami_id, target="cloud", age_seconds=0)

    assert _cloud_claim(db) == job_id
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, runner_id, heartbeat_at IS NOT NULL FROM brain_jobs WHERE id = %s",
            (job_id,),
        )
        assert cur.fetchone() == ("running", "cloud-a", True)


def test_an_auto_job_waits_for_a_pc_that_is_there(db):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _pc_seen(db, user_id, seconds_ago=30)
    _job(db, user_id, kwami_id)
    assert _cloud_claim(db) is None


def test_an_auto_job_goes_to_the_cloud_once_the_pc_is_gone_and_patience_is_spent(db):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _pc_seen(db, user_id, seconds_ago=120)
    fresh = _job(db, user_id, kwami_id, age_seconds=5)
    assert _cloud_claim(db) is None, "a PC switching on gets the first go"

    old = _job(db, user_id, kwami_id)
    assert _cloud_claim(db) == old
    with db.cursor() as cur:
        cur.execute("SELECT status FROM brain_jobs WHERE id = %s", (fresh,))
        assert cur.fetchone() == ("queued",)


def test_a_job_for_the_pc_never_goes_to_the_cloud(db):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _job(db, user_id, kwami_id, target="pc", age_seconds=86400)
    assert _cloud_claim(db) is None


@pytest.mark.parametrize("mode", ["think", "research", "merge", "login", "site"])
def test_thinking_and_orders_never_go_to_the_cloud(db, mode):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _job(db, user_id, kwami_id, target="cloud", mode=mode)
    assert _cloud_claim(db) is None


def test_the_pc_leaves_what_was_asked_of_the_cloud(db):
    user_id, kwami_id = _user(db)
    _job(db, user_id, kwami_id, target="cloud")
    for_pc = _job(db, user_id, kwami_id, target="pc")
    assert _pc_claim(db, user_id) == for_pc
    assert _pc_claim(db, user_id) is None


def test_the_pc_still_takes_auto_work_and_its_thinking_line_is_unchanged(db):
    user_id, kwami_id = _user(db)
    work = _job(db, user_id, kwami_id)
    thought = _job(db, user_id, kwami_id, mode="think")
    assert _pc_claim(db, user_id, thinking=True) == thought
    assert _pc_claim(db, user_id) == work


def test_one_user_never_holds_more_than_their_share_of_the_cloud(db):
    busy, busy_kwami = _user(db)
    other, other_kwami = _user(db)
    for user_id in (busy, other):
        _allow_cloud(db, user_id)
    _job(db, busy, busy_kwami, status="running", runner_id="cloud-a")
    _job(db, busy, busy_kwami, status="running", runner_id="cloud-b")
    # Older than the other user's, and still not taken.
    _job(db, busy, busy_kwami, age_seconds=PATIENCE + 50)
    theirs = _job(db, other, other_kwami)

    assert _cloud_claim(db) == theirs
    assert _cloud_claim(db) is None


def test_work_running_on_the_pc_does_not_count_against_the_cloud_share(db):
    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    _job(db, user_id, kwami_id, status="running", runner_id="pc-1")
    job_id = _job(db, user_id, kwami_id, target="cloud")
    assert _cloud_claim(db, per_user=1) == job_id


def test_two_cloud_workers_never_take_the_same_job(migrated_database, db):
    import psycopg

    user_id, kwami_id = _user(db)
    _allow_cloud(db, user_id)
    for _ in range(4):
        _job(db, user_id, kwami_id, target="cloud")

    def claim(runner: str) -> str | None:
        with psycopg.connect(migrated_database, autocommit=True) as conn:
            return _cloud_claim(conn, runner, per_user=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        taken = [t for t in pool.map(claim, [f"cloud-{i}" for i in range(8)]) if t]

    assert len(taken) == 4
    assert len(set(taken)) == 4


def test_a_sync_is_applied_by_one_pc_only(db):
    user_id, kwami_id = _user(db)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO brain_sync (user_id, kwami_id, kind, storage_prefix)
            VALUES (%s, %s, 'files', 'u/k/t')
            RETURNING id
            """,
            (user_id, kwami_id),
        )
        sync_id = str(cur.fetchone()[0])
        cur.execute("SELECT id, status, runner_id FROM brain_claim_sync(%s, 'pc-1')", (user_id,))
        assert cur.fetchone() == (uuid.UUID(sync_id), "applying", "pc-1")
        cur.execute("SELECT id FROM brain_claim_sync(%s, 'pc-2')", (user_id,))
        assert cur.fetchone() is None


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_the_public_roles_cannot_claim_or_write(db, role):
    import psycopg

    user_id, kwami_id = _user(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {role}")
        try:
            for statement, params in (
                ("SELECT brain_claim_cloud_job('cloud-x')", ()),
                ("SELECT brain_claim_sync(%s, 'pc-x')", (user_id,)),
                (
                    "INSERT INTO brain_cloud_settings (user_id, enabled) VALUES (%s, true)",
                    (user_id,),
                ),
                ("INSERT INTO brain_cloud_runners (runner_id) VALUES ('cloud-x')", ()),
                (
                    "INSERT INTO brain_agents (user_id, name, description, prompt) "
                    "VALUES (%s, 'spy', 'd', 'p')",
                    (user_id,),
                ),
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(statement, params)
        finally:
            cur.execute("RESET ROLE")


def test_an_agent_name_must_be_a_slug(db):
    import psycopg

    user_id, _ = _user(db)
    with db.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO brain_agents (user_id, name, description, prompt) "
            "VALUES (%s, 'Not A Slug', 'd', 'p')",
            (user_id,),
        )


def test_a_deleted_agent_leaves_its_jobs_and_schedules_in_place(db):
    user_id, kwami_id = _user(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO brain_agents (user_id, name, description, prompt) "
            "VALUES (%s, 'facturas', 'd', 'p') RETURNING id",
            (user_id,),
        )
        agent_id = cur.fetchone()[0]
        job_id = _job(db, user_id, kwami_id)
        cur.execute("UPDATE brain_jobs SET agent_id = %s WHERE id = %s", (agent_id, job_id))
        cur.execute("DELETE FROM brain_agents WHERE id = %s", (agent_id,))
        cur.execute("SELECT agent_id FROM brain_jobs WHERE id = %s", (job_id,))
        assert cur.fetchone() == (None,)
