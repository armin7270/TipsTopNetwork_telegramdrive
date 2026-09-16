-- =============================================================================
-- TeleDrive :: 0001_init.sql
-- Core schema for the Virtual File System + Telegram chunk storage index.
-- Target: PostgreSQL 16+ (uses UNIQUE NULLS NOT DISTINCT, pgcrypto, pg_trgm).
-- All statements are idempotent so the migration runner can be re-entrant.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), digest(), hmac()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram indexes for node name search
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email

-- -----------------------------------------------------------------------------
-- Enumerated types
-- -----------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE node_kind AS ENUM ('folder', 'file');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- A file row is created the moment an upload session starts, so that the
-- upload target has a stable identity (node_id) before any chunk exists.
-- Only 'ready' nodes are visible to listing/search endpoints.
DO $$ BEGIN
    CREATE TYPE upload_state AS ENUM ('uploading', 'ready', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE upload_session_status AS ENUM
        ('pending', 'assembling', 'completed', 'aborted', 'expired');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- server_managed : the API derives a per-file key from the user's master key,
--                  so the server can decrypt (needed for server-side previews).
-- zero_knowledge : the client supplies the key; the server only ever sees
--                  ciphertext and cannot produce previews.
DO $$ BEGIN
    CREATE TYPE encryption_mode AS ENUM ('server_managed', 'zero_knowledge');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE session_state AS ENUM
        ('healthy', 'floodwait', 'quarantined', 'disabled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- How the per-file digest in nodes.sha256 was produced. Storing a plaintext
-- SHA-256 in zero_knowledge mode would defeat the point (it is a confirmation
-- oracle for low-entropy content), so ZK uploads store a client-keyed HMAC.
DO $$ BEGIN
    CREATE TYPE hash_mode AS ENUM ('plaintext_sha256', 'client_hmac', 'none');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- -----------------------------------------------------------------------------
-- updated_at maintenance
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- =============================================================================
-- 1. Identity, credentials, sessions
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email               citext UNIQUE,
    password_hash       text,          -- Argon2id encoded string; NULL for TMA-only users
    display_name        text NOT NULL DEFAULT '',
    role                text NOT NULL DEFAULT 'user'
                            CHECK (role IN ('user', 'admin')),
    status              text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'suspended', 'deleted')),

    -- Telegram identity for Mini App logins (initData.user.id)
    telegram_user_id    bigint UNIQUE,
    telegram_username   text,

    -- 0 means "unlimited". used_bytes is a cached counter maintained by trigger
    -- (see 0002_functions.sql) and reconciled nightly against nodes.
    quota_bytes         bigint NOT NULL DEFAULT 0 CHECK (quota_bytes >= 0),
    used_bytes          bigint NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),

    -- Per-user Data Encryption Key, wrapped with the server MASTER_KEK.
    -- This is the root of the server_managed encryption hierarchy. It is never
    -- stored in plaintext and never leaves the process in unwrapped form.
    dek_wrapped         bytea,
    dek_version         integer NOT NULL DEFAULT 1,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT users_identity_present
        CHECK (email IS NOT NULL OR telegram_user_id IS NOT NULL)
);

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Refresh tokens are stored hashed and grouped into a "family". Reuse of a
-- token that has already been rotated revokes the entire family, which is the
-- standard defence against refresh-token replay after theft.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash      bytea PRIMARY KEY,          -- sha256 of the opaque token
    family_id       uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    used_at         timestamptz,                -- set when rotated
    revoked_at      timestamptz,
    replaced_by     bytea,                      -- token_hash of the successor
    user_agent      text,
    ip              inet
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user
    ON refresh_tokens (user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family
    ON refresh_tokens (family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expiry
    ON refresh_tokens (expires_at);

-- =============================================================================
-- 2. Telegram storage substrate: pools, channels, account sessions
-- =============================================================================

-- A "pool" is a private channel/supergroup used as a blob bucket. Sharding
-- across several pools is the primary defence against per-chat rate limits
-- (roughly 20 messages/minute/chat) - see docs/06-telegram-ban-avoidance.md.
CREATE TABLE IF NOT EXISTS storage_pools (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label                   text NOT NULL UNIQUE,
    telegram_channel_id     bigint NOT NULL UNIQUE,   -- marked id, e.g. -1001234567890
    access_hash             bigint,
    is_channel              boolean NOT NULL DEFAULT true,
    dc_id                   integer,
    used_bytes              bigint NOT NULL DEFAULT 0,
    chunk_count             bigint NOT NULL DEFAULT 0,
    max_bytes               bigint NOT NULL DEFAULT 0,  -- 0 = no soft cap
    weight                  integer NOT NULL DEFAULT 100 CHECK (weight > 0),
    is_active               boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_storage_pools_updated_at ON storage_pools;
CREATE TRIGGER trg_storage_pools_updated_at BEFORE UPDATE ON storage_pools
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Authorized MTProto accounts. The Telethon StringSession is a bearer
-- credential equivalent to a logged-in device; it is encrypted at rest with
-- the MASTER_KEK and is never returned by any API surface.
CREATE TABLE IF NOT EXISTS telegram_sessions (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label                   text NOT NULL UNIQUE,
    api_id                  integer NOT NULL,
    phone_masked            text,               -- e.g. +1 555 *** 1234, display only
    session_enc             bytea NOT NULL,     -- AES-256-GCM(MASTER_KEK, StringSession)
    session_fingerprint     bytea NOT NULL,     -- sha256(plaintext) for change detection
    dc_id                   integer,
    state                   session_state NOT NULL DEFAULT 'healthy',
    floodwait_until         timestamptz,        -- hard gate; no traffic before this
    consecutive_failures    integer NOT NULL DEFAULT 0,
    quarantine_until        timestamptz,
    last_error              text,
    last_used_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_sessions_selectable
    ON telegram_sessions (state, floodwait_until);

DROP TRIGGER IF EXISTS trg_telegram_sessions_updated_at ON telegram_sessions;
CREATE TRIGGER trg_telegram_sessions_updated_at BEFORE UPDATE ON telegram_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 3. Virtual File System: nodes
-- =============================================================================
--
-- Telegram has no hierarchy, so the tree lives entirely here. Design notes:
--
--  * parent_id is the adjacency list (source of truth for the tree shape).
--  * ancestor_ids is a materialised array of ancestor node ids ordered root
--    first, EXCLUDING the node itself. It makes breadcrumbs and subtree
--    queries index-driven instead of recursive:
--        breadcrumb : SELECT ... WHERE id = ANY(ancestor_ids) ORDER BY depth
--        subtree    : WHERE ancestor_ids @> ARRAY[$node_id]
--    The array is rewritten by a trigger when a subtree is moved.
--  * Only the root node has parent_id IS NULL, so the uniqueness index below
--    can use NULLS NOT DISTINCT (PG15+) and still allow user-level roots.
--
CREATE TABLE IF NOT EXISTS nodes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id           uuid REFERENCES nodes(id) ON DELETE RESTRICT,

    kind                node_kind NOT NULL,
    -- Root node is named '/'. User-visible names may not contain '/' or NUL.
    name                text NOT NULL CHECK (
                            char_length(name) BETWEEN 1 AND 255
                            AND position('/' in name) = 0
                            AND position(chr(0) in name) = 0
                        ),
    -- Case-insensitive uniqueness without needing the citext extension on nodes.
    name_folded         text GENERATED ALWAYS AS (lower(name)) STORED,

    ancestor_ids        uuid[] NOT NULL DEFAULT '{}',
    depth               integer NOT NULL DEFAULT 0 CHECK (depth >= 0),

    -- --- file-only payload metadata (NULL/0 for folders) -------------------
    size_bytes          bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    mime_type           text,
    sha256              bytea,                 -- digest of the plaintext, 32 bytes
    hash_mode           hash_mode NOT NULL DEFAULT 'none',
    chunk_size          integer CHECK (
                            chunk_size IS NULL
                            OR (chunk_size >= 1048576
                                AND chunk_size <= 2147483648
                                AND chunk_size % 1048576 = 0)
                        ),
    total_chunks        integer NOT NULL DEFAULT 0 CHECK (total_chunks >= 0),
    encryption_mode     encryption_mode NOT NULL DEFAULT 'server_managed',
    -- Non-secret crypto parameters: cipher, kdf, salt, base nonce, key version.
    encryption_meta     jsonb NOT NULL DEFAULT '{}'::jsonb,

    upload_state        upload_state NOT NULL DEFAULT 'ready',

    is_starred          boolean NOT NULL DEFAULT false,
    trashed_at          timestamptz,
    trash_parent_id     uuid REFERENCES nodes(id) ON DELETE SET NULL,
    purge_after         timestamptz,

    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    version             integer NOT NULL DEFAULT 1,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- Enforce that folders never carry payload fields and files always do.
    CONSTRAINT nodes_kind_shape CHECK (
        (kind = 'folder' AND size_bytes = 0 AND sha256 IS NULL
                          AND chunk_size IS NULL AND total_chunks = 0)
        OR
        (kind = 'file')
    ),
    -- Trash bookkeeping must be internally consistent.
    CONSTRAINT nodes_trash_shape CHECK (
        (trashed_at IS NULL AND purge_after IS NULL)
        OR (trashed_at IS NOT NULL)
    )
);

-- Sibling uniqueness among live nodes. NULLS NOT DISTINCT is what makes the
-- single root per user (parent_id IS NULL) work.
CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_sibling_name
    ON nodes (owner_id, parent_id, name_folded) NULLS NOT DISTINCT
    WHERE trashed_at IS NULL;

-- Materialised-path indexes.
CREATE INDEX IF NOT EXISTS idx_nodes_ancestors
    ON nodes USING gin (ancestor_ids);
CREATE INDEX IF NOT EXISTS idx_nodes_owner_parent
    ON nodes (owner_id, parent_id, kind, name_folded)
    WHERE trashed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_owner_depth
    ON nodes (owner_id, depth);
CREATE INDEX IF NOT EXISTS idx_nodes_trash_purge
    ON nodes (purge_after) WHERE trashed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_pending_uploads
    ON nodes (owner_id, created_at) WHERE upload_state <> 'ready';
CREATE INDEX IF NOT EXISTS idx_nodes_starred
    ON nodes (owner_id, updated_at DESC) WHERE is_starred AND trashed_at IS NULL;
-- Trigram search over names, scoped by owner at query time.
CREATE INDEX IF NOT EXISTS idx_nodes_name_trgm
    ON nodes USING gin (name_folded gin_trgm_ops) WHERE trashed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_mime
    ON nodes (owner_id, mime_type) WHERE kind = 'file' AND trashed_at IS NULL;

DROP TRIGGER IF EXISTS trg_nodes_updated_at ON nodes;
CREATE TRIGGER trg_nodes_updated_at BEFORE UPDATE ON nodes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 4. Chunk index: the mapping from logical chunks to Telegram messages
-- =============================================================================

-- file_chunks holds the LOGICAL chunk and a denormalised pointer to its
-- PRIMARY physical location. The Telegram columns are duplicated from
-- chunk_replicas deliberately: the streaming download hot path joins one row
-- per chunk and must not pay for an extra join per 64 MiB read. chunk_replicas
-- then provides optional additional copies for durability/failover.
CREATE TABLE IF NOT EXISTS file_chunks (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id                 uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    chunk_index             integer NOT NULL CHECK (chunk_index >= 0),

    plaintext_size          integer NOT NULL CHECK (plaintext_size > 0),
    ciphertext_size         integer NOT NULL CHECK (ciphertext_size >= plaintext_size),
    -- Plaintext chunk digest. Powers upload resume (client sends it as
    -- X-Chunk-SHA256) and post-upload verification.
    sha256                  bytea NOT NULL,

    -- AES-256-GCM parameters. A unique IV per chunk is mandatory; reusing an
    -- IV with the same key destroys GCM confidentiality and authenticity.
    iv                      bytea NOT NULL CHECK (octet_length(iv) = 12),
    auth_tag                bytea CHECK (auth_tag IS NULL OR octet_length(auth_tag) = 16),
    aad_version             smallint NOT NULL DEFAULT 1,
    crypto_version          smallint NOT NULL DEFAULT 1,

    -- --- primary physical location -----------------------------------------
    storage_pool_id         uuid NOT NULL REFERENCES storage_pools(id),
    telegram_channel_id     bigint NOT NULL,
    telegram_message_id     bigint NOT NULL,
    telegram_file_id        text,          -- stable Bot-API/TDLib style file id
    telegram_access_hash    bigint,
    telegram_dc_id          integer,
    telegram_file_size      bigint,

    uploaded_by_session     uuid REFERENCES telegram_sessions(id) ON DELETE SET NULL,
    uploaded_at             timestamptz NOT NULL DEFAULT now(),
    verified_at             timestamptz,

    CONSTRAINT uq_file_chunks_node_index UNIQUE (node_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_file_chunks_node
    ON file_chunks (node_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_file_chunks_message
    ON file_chunks (telegram_channel_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_file_chunks_pool
    ON file_chunks (storage_pool_id, uploaded_at);

-- Optional extra copies. A row here means "this chunk byte-identical copy also
-- exists at <location>". Reads may fail over to a replica when the primary
-- message has been deleted or the source session is quarantined.
CREATE TABLE IF NOT EXISTS chunk_replicas (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id                uuid NOT NULL REFERENCES file_chunks(id) ON DELETE CASCADE,
    storage_pool_id         uuid NOT NULL REFERENCES storage_pools(id),
    telegram_channel_id     bigint NOT NULL,
    telegram_message_id     bigint NOT NULL,
    telegram_file_id        text,
    telegram_access_hash    bigint,
    telegram_dc_id          integer,
    is_primary              boolean NOT NULL DEFAULT false,
    created_at              timestamptz NOT NULL DEFAULT now(),
    verified_at             timestamptz,
    CONSTRAINT uq_chunk_replicas_location
        UNIQUE (chunk_id, telegram_channel_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chunk_replicas_chunk
    ON chunk_replicas (chunk_id) WHERE NOT is_primary;

-- Read-failover state: a chunk can be marked unreadable so the downloader
-- skips straight to a replica instead of retrying a dead message.
CREATE TABLE IF NOT EXISTS chunk_read_failures (
    chunk_id        uuid NOT NULL REFERENCES file_chunks(id) ON DELETE CASCADE,
    message_id      bigint NOT NULL,
    failure_count   integer NOT NULL DEFAULT 1,
    last_error      text,
    blocked_until   timestamptz,
    PRIMARY KEY (chunk_id, message_id)
);

-- =============================================================================
-- 5. Resumable upload sessions
-- =============================================================================

CREATE TABLE IF NOT EXISTS upload_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    node_id             uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    parent_id           uuid REFERENCES nodes(id) ON DELETE SET NULL,

    name_folded         text NOT NULL,
    size_bytes          bigint NOT NULL CHECK (size_bytes >= 0),
    mime_type           text,
    expected_sha256     bytea,
    hash_mode           hash_mode NOT NULL DEFAULT 'plaintext_sha256',
    chunk_size          integer NOT NULL CHECK (
                            chunk_size >= 1048576
                            AND chunk_size <= 2147483648
                            AND chunk_size % 1048576 = 0
                        ),
    total_chunks        integer NOT NULL CHECK (total_chunks >= 0),
    encryption_mode     encryption_mode NOT NULL DEFAULT 'server_managed',

    received_chunks     integer NOT NULL DEFAULT 0,
    received_bytes      bigint NOT NULL DEFAULT 0,

    status              upload_session_status NOT NULL DEFAULT 'pending',
    idempotency_key     uuid,
    reserved_bytes      bigint NOT NULL DEFAULT 0,

    expires_at          timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz
);

-- Scoped idempotency: a retried POST /uploads with the same key must return the
-- existing session rather than creating a second one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_upload_sessions_idempotency
    ON upload_sessions (owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_upload_sessions_owner_status
    ON upload_sessions (owner_id, status);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_reap
    ON upload_sessions (expires_at)
    WHERE status IN ('pending', 'assembling');

DROP TRIGGER IF EXISTS trg_upload_sessions_updated_at ON upload_sessions;
CREATE TRIGGER trg_upload_sessions_updated_at BEFORE UPDATE ON upload_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 6. Audit trail and distributed background jobs
-- =============================================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    actor_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
    action          text NOT NULL,          -- e.g. 'node.delete', 'auth.login'
    target_type     text,
    target_id       uuid,
    outcome         text NOT NULL DEFAULT 'success'
                        CHECK (outcome IN ('success', 'failure', 'denied')),
    ip              inet,
    user_agent      text,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_time
    ON audit_log (actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action_time
    ON audit_log (action, created_at DESC);

-- Lease-based work queue. Postgres is the durable record; Redis handles fast
-- coordination. This survives a full Redis flush without losing garbage
-- collection of orphaned chunks.
CREATE TABLE IF NOT EXISTS background_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,             -- 'purge_trash', 'gc_orphans', ...
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_at          timestamptz NOT NULL DEFAULT now(),
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 5,
    locked_by       text,
    locked_until    timestamptz,
    last_error      text,
    completed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_background_jobs_claimable
    ON background_jobs (run_at, created_at)
    WHERE completed_at IS NULL;

COMMIT;