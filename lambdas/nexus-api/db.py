"""Database helper for nexus-api — PostgreSQL channel management.

Reads credentials from Secrets Manager (cached), provides connection helper
and auto-creates the channels table on first use.
"""

import json
import logging

import boto3
import psycopg2
import psycopg2.extras
import psycopg2.pool

log = logging.getLogger("nexus-api.db")

_cache: dict = {}
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


class _PooledConnection:
    def __init__(self, pool: psycopg2.pool.ThreadedConnectionPool, conn):
        self._pool = pool
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *args):
        result = self._conn.__exit__(*args)
        return result

    def close(self):
        self._pool.putconn(self._conn)

CHANNELS_DDL = """
CREATE TABLE IF NOT EXISTS nexus_channels (
    channel_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    niche        TEXT NOT NULL,
    profile      TEXT NOT NULL DEFAULT 'documentary',
    style_hints  TEXT DEFAULT '',
    voice_id     TEXT DEFAULT '',
    brand        JSONB DEFAULT '{}',
    schedule     JSONB DEFAULT '{}',
    stats        JSONB DEFAULT '{"videos_generated": 0, "status": "setting_up"}',
    status       TEXT NOT NULL DEFAULT 'setting_up',
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_channels_status ON nexus_channels(status);
"""

# Add channel_id column to nexus_runs if it exists (nullable, no FK)
RUNS_MIGRATION = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'nexus_runs')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'nexus_runs' AND column_name = 'channel_id')
    THEN
        ALTER TABLE nexus_runs ADD COLUMN channel_id TEXT;
    END IF;
END $$;
"""


def _get_db_credentials() -> dict:
    """Fetch DB credentials from Secrets Manager, cached."""
    if "db_creds" not in _cache:
        sm = boto3.client("secretsmanager")
        secret = json.loads(
            sm.get_secret_value(SecretId="nexus/db_credentials")["SecretString"]
        )
        _cache["db_creds"] = secret
    return _cache["db_creds"]


def get_connection():
    global _pool
    creds = _get_db_credentials()
    dbname = creds.get("dbname") or "nexus"
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            host=creds["host"],
            port=creds.get("port", 5432),
            dbname=dbname,
            user=creds["user"],
            password=creds["password"],
            connect_timeout=10,
        )
    conn = _pool.getconn()
    if conn.closed:
        _pool.putconn(conn, close=True)
        conn = _pool.getconn()
    return _PooledConnection(_pool, conn)


def bootstrap_schema():
    """Ensure the channels table and related migrations exist."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(CHANNELS_DDL)
                cur.execute(RUNS_MIGRATION)
        log.info("Database schema bootstrapped")
    finally:
        conn.close()


def list_channels(status_filter: str | None = None) -> list[dict]:
    """Return all channels, optionally filtered by status."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if status_filter:
                    cur.execute(
                        "SELECT * FROM nexus_channels WHERE status = %s ORDER BY created_at DESC",
                        (status_filter,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM nexus_channels WHERE status != 'archived' ORDER BY created_at DESC"
                    )
                return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_channel(channel_id: str) -> dict | None:
    """Return a single channel by ID, or None."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM nexus_channels WHERE channel_id = %s", (channel_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def create_channel(channel_id: str, name: str, niche: str, profile: str, style_hints: str = "", schedule: dict | None = None) -> dict:
    """Insert a new channel row. Returns the created row."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Ensure table exists
                cur.execute(CHANNELS_DDL)
                cur.execute(
                    """
                    INSERT INTO nexus_channels (channel_id, name, niche, profile, style_hints, schedule, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'setting_up')
                    RETURNING *
                    """,
                    (channel_id, name, niche, profile, style_hints, json.dumps(schedule or {})),
                )
                return dict(cur.fetchone())
    finally:
        conn.close()


def update_channel_brand(channel_id: str, brand: dict, voice_id: str = "", status: str = "active") -> dict | None:
    """Update brand kit, voice_id, and status for a channel."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE nexus_channels
                    SET brand = %s, voice_id = %s, status = %s,
                        stats = jsonb_set(COALESCE(stats, '{}'), '{status}', %s::jsonb),
                        updated_at = NOW()
                    WHERE channel_id = %s
                    RETURNING *
                    """,
                    (json.dumps(brand), voice_id, status, json.dumps(status), channel_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def update_channel_status(channel_id: str, status: str) -> None:
    """Update just the status field."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE nexus_channels SET status = %s, updated_at = NOW() WHERE channel_id = %s",
                    (status, channel_id),
                )
    finally:
        conn.close()


def update_channel_settings(channel_id: str, name: str | None = None, niche: str | None = None) -> dict | None:
    """Update channel name/niche."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                sets = []
                vals = []
                if name is not None:
                    sets.append("name = %s")
                    vals.append(name)
                if niche is not None:
                    sets.append("niche = %s")
                    vals.append(niche)
                if not sets:
                    return get_channel(channel_id)
                sets.append("updated_at = NOW()")
                vals.append(channel_id)
                cur.execute(
                    f"UPDATE nexus_channels SET {', '.join(sets)} WHERE channel_id = %s RETURNING *",
                    vals,
                )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def archive_channel(channel_id: str) -> bool:
    """Set channel status to archived. Returns True if found."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE nexus_channels SET status = 'archived', updated_at = NOW() WHERE channel_id = %s",
                    (channel_id,),
                )
                return cur.rowcount > 0
    finally:
        conn.close()


def get_channel_videos(channel_id: str, limit: int = 50) -> list[dict]:
    """Fetch videos (nexus_runs rows) linked to a channel."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Check if channel_id column exists in nexus_runs
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'nexus_runs' AND column_name = 'channel_id'
                    """
                )
                if cur.fetchone():
                    cur.execute(
                        """
                        SELECT run_id, niche, profile, title, duration_sec, video_url,
                               elapsed_sec, created_at, channel_id
                        FROM nexus_runs
                        WHERE channel_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (channel_id, limit),
                    )
                else:
                    # Fallback: return empty since we can't filter
                    return []
                return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def increment_video_count(channel_id: str) -> None:
    """Bump videos_generated in stats JSONB."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE nexus_channels
                    SET stats = jsonb_set(
                        COALESCE(stats, '{}'),
                        '{videos_generated}',
                        (COALESCE((stats->>'videos_generated')::int, 0) + 1)::text::jsonb
                    ),
                    updated_at = NOW()
                    WHERE channel_id = %s
                    """,
                    (channel_id,),
                )
    finally:
        conn.close()

