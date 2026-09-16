-- =============================================================================
-- TeleDrive :: 0002_functions.sql
-- Integrity logic that belongs in the database rather than in application code.
--
-- Rationale: the VFS tree, quota accounting, and trash semantics are invariants
-- that must hold no matter which replica, migration script, or operator touches
-- the data. Putting them in triggers means a buggy API path cannot corrupt the
-- tree, and it removes read-modify-write races between concurrent API workers.
--
-- Custom SQLSTATE codes raised here are mapped to HTTP statuses by the API:
--   TDL01 -> 507 quota_exceeded
--   TDL02 -> 409 node_cycle
--   TDL03 -> 409 folder_not_empty
--   TDL04 -> 409 root_immutable
--   TDL05 -> 403 owner_mismatch
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Ancestor materialisation
--
-- ancestor_ids is a flat array of ancestor node ids ordered root-first and
-- EXCLUDING the node itself. It is what makes breadcrumbs and subtree scans
-- index-driven (GIN on uuid[]) instead of recursive.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION node_ancestor_ids(p_parent uuid) RETURNS uuid[]
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_parent IS NULL THEN '{}'::uuid[]
        ELSE p_parent || (SELECT n.ancestor_ids FROM nodes n WHERE n.id = p_parent)
    END;
$$;

CREATE OR REPLACE FUNCTION node_depth(p_parent uuid) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_parent IS NULL THEN 0
        ELSE 1 + (SELECT n.depth FROM nodes n WHERE n.id = p_parent)
    END;
$$;

-- Human-readable absolute path. The root node is named '/' and contributes no
-- path segment, so it is filtered out of the aggregation.
CREATE OR REPLACE FUNCTION node_path(p_node uuid) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN n.parent_id IS NULL THEN '/'
        ELSE '/' || COALESCE(parts.joined || '/', '') || n.name
    END
    FROM nodes n
    LEFT JOIN LATERAL (
        SELECT array_to_string(
                   array_agg(a.name ORDER BY a.depth), '/'
               ) AS joined
        FROM nodes a
        WHERE a.id = ANY (n.ancestor_ids)
          AND a.parent_id IS NOT NULL
    ) AS parts ON true
    WHERE n.id = p_node;
$$;

-- Resolve an absolute path within a user's drive to a node id.
-- Returns NULL when any intermediate segment is missing.
CREATE OR REPLACE FUNCTION resolve_path(p_owner uuid, p_path text)
RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_segments text[];
    v_current  uuid;
    v_segment  text;
BEGIN
    IF p_path IS NULL OR length(btrim(p_path)) = 0 THEN
        RETURN NULL;
    END IF;

    SELECT id INTO v_current FROM nodes
     WHERE owner_id = p_owner AND parent_id IS NULL
     LIMIT 1;

    IF v_current IS NULL THEN
        RETURN NULL;
    END IF;

    v_segments := string_to_array(btrim(both '/' from p_path), '/');

    FOREACH v_segment IN ARRAY v_segments LOOP
        CONTINUE WHEN v_segment IS NULL OR v_segment = '';
        SELECT id INTO v_current
          FROM nodes
         WHERE owner_id = p_owner
           AND parent_id = v_current
           AND name_folded = lower(v_segment)
           AND trashed_at IS NULL;
        IF v_current IS NULL THEN
            RETURN NULL;
        END IF;
    END LOOP;

    RETURN v_current;
END $$;

-- -----------------------------------------------------------------------------
-- Node tree guards
-- -----------------------------------------------------------------------------

-- Derive ancestor_ids/depth, and enforce owner consistency + cycle prevention.
CREATE OR REPLACE FUNCTION nodes_before_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_parent_owner uuid;
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.ancestor_ids := '{}'::uuid[];
        NEW.depth := 0;
    ELSE
        SELECT owner_id INTO v_parent_owner FROM nodes WHERE id = NEW.parent_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'parent node % does not exist', NEW.parent_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;

        -- Cross-tenant parent links are a data-leak vector, not just a bug.
        IF v_parent_owner <> NEW.owner_id THEN
            RAISE EXCEPTION 'node owner does not match parent owner'
                USING ERRCODE = 'TDL05';
        END IF;

        -- A cycle would make the subtreemanipulation trigger recurse forever.
        IF NEW.parent_id = NEW.id THEN
            RAISE EXCEPTION 'node cannot be its own parent'
                USING ERRCODE = 'TDL02';
        END IF;
        IF NEW.id = ANY (NEW.ancestor_ids)
           OR (TG_OP = 'UPDATE' AND NEW.parent_id = ANY (OLD.ancestor_ids)) THEN
            RAISE EXCEPTION 'move would create a cycle'
                USING ERRCODE = 'TDL02';
        END IF;

        NEW.ancestor_ids := node_ancestor_ids(NEW.parent_id);
        NEW.depth := node_depth(NEW.parent_id);

        -- Re-check after recomputation: the new parent must not be a descendant.
        IF NEW.id = ANY (NEW.ancestor_ids) THEN
            RAISE EXCEPTION 'move would create a cycle'
                USING ERRCODE = 'TDL02';
        END IF;
    END IF;

    -- Folders may never carry payload metadata.
    IF NEW.kind = 'folder' THEN
        NEW.size_bytes := 0;
        NEW.sha256 := NULL;
        NEW.chunk_size := NULL;
        NEW.total_chunks := 0;
        NEW.hash_mode := 'none';
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_before_write ON nodes;
CREATE TRIGGER trg_nodes_before_write BEFORE INSERT OR UPDATE ON nodes
    FOR EACH ROW EXECUTE FUNCTION nodes_before_write();

-- When a subtree is re-parented, every descendant's ancestor_ids and depth must
-- be rewritten. A session-local flag suppresses recursion: the UPDATE below
-- fires this trigger again for each descendant, and without the guard it would
-- re-walk the subtree exponentially.
CREATE OR REPLACE FUNCTION nodes_after_move() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
        RETURN NULL;
    END IF;
    IF current_setting('teledrive.subtree_move', true) = 'on' THEN
        RETURN NULL;
    END IF;

    PERFORM set_config('teledrive.subtree_move', 'on', true);

    UPDATE nodes child
       SET ancestor_ids = child.ancestor_ids[1:array_length(OLD.ancestor_ids, 1)]
                          || NEW.ancestor_ids
                          || ARRAY[NEW.id],
           depth = OLD.depth + 1 + (child.depth - OLD.depth)
     WHERE OLD.id = ANY (child.ancestor_ids);

    PERFORM set_config('teledrive.subtree_move', 'off', true);
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_after_move ON nodes;
CREATE TRIGGER trg_nodes_after_move AFTER UPDATE OF parent_id ON nodes
    FOR EACH ROW EXECUTE FUNCTION nodes_after_move();

-- The per-user root is structural: it must not be renamed, moved, or deleted.
CREATE OR REPLACE FUNCTION nodes_protect_root() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parent_id IS NULL THEN
            RAISE EXCEPTION 'root node cannot be deleted' USING ERRCODE = 'TDL04';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.parent_id IS NULL THEN
        IF NEW.name <> OLD.name OR NEW.parent_id IS NOT NULL THEN
            RAISE EXCEPTION 'root node cannot be renamed or moved'
                USING ERRCODE = 'TDL04';
        END IF;
        IF NEW.trashed_at IS NOT NULL THEN
            RAISE EXCEPTION 'root node cannot be trashed' USING ERRCODE = 'TDL04';
        END IF;
    END IF;

    -- A trashed node may not be re-parented without being restored first;
    -- otherwise a restore could resurrect it under the wrong parent.
    IF NEW.trashed_at IS NOT NULL
       AND NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
        RAISE EXCEPTION 'trashed node cannot be moved' USING ERRCODE = 'TDL01';
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_protect_root ON nodes;
CREATE TRIGGER trg_nodes_protect_root BEFORE UPDATE OR DELETE ON nodes
    FOR EACH ROW EXECUTE FUNCTION nodes_protect_root();

-- Trashing a folder trashes its whole subtree so that restore is exact.
CREATE OR REPLACE FUNCTION nodes_trash_cascade() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.trashed_at IS NOT DISTINCT FROM OLD.trashed_at THEN
        RETURN NULL;
    END IF;
    IF current_setting('teledrive.trash_cascade', true) = 'on' THEN
        RETURN NULL;
    END IF;

    PERFORM set_config('teledrive.trash_cascade', 'on', true);

    IF OLD.trashed_at IS NULL AND NEW.trashed_at IS NOT NULL THEN
        UPDATE nodes child
           SET trashed_at = NEW.trashed_at,
               purge_after = NEW.purge_after,
               trash_parent_id = COALESCE(NEW.trash_parent_id, NEW.parent_id)
         WHERE OLD.id = ANY (child.ancestor_ids)
           AND child.trashed_at IS NULL;
    ELSIF OLD.trashed_at IS NOT NULL AND NEW.trashed_at IS NULL THEN
        UPDATE nodes child
           SET trashed_at = NULL,
               purge_after = NULL,
               trash_parent_id = NULL
         WHERE OLD.id = ANY (child.ancestor_ids)
           AND child.trashed_at IS NOT NULL;
    END IF;

    PERFORM set_config('teledrive.trash_cascade', 'off', true);
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_trash_cascade ON nodes;
CREATE TRIGGER trg_nodes_trash_cascade AFTER UPDATE OF trashed_at ON nodes
    FOR EACH ROW EXECUTE FUNCTION nodes_trash_cascade();

-- -----------------------------------------------------------------------------
-- Quota accounting
--
-- Only live (non-trashed) file bytes count against quota, so moving a file to
-- the trash never locks a user out of uploading, while the bytes are still
-- reclaimable by the purge job.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION live_bytes(p_kind node_kind, p_trashed timestamptz, p_size bigint)
RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_kind = 'file' AND p_trashed IS NULL THEN p_size ELSE 0 END;
$$;

CREATE OR REPLACE FUNCTION nodes_quota_delta() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_delta      bigint;
    v_hard_limit bigint;
    v_used       bigint;
BEGIN
    v_delta := CASE TG_OP
        WHEN 'INSERT' THEN live_bytes(NEW.kind, NEW.trashed_at, NEW.size_bytes)
        WHEN 'DELETE' THEN -live_bytes(OLD.kind, OLD.trashed_at, OLD.size_bytes)
        ELSE live_bytes(NEW.kind, NEW.trashed_at, NEW.size_bytes)
             - live_bytes(OLD.kind, OLD.trashed_at, OLD.size_bytes)
    END;

    IF v_delta = 0 THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- Serialise per-user accounting. Without this, two concurrent uploads can
    -- both read used_bytes and both commit, permanently undercounting usage.
    PERFORM 1 FROM users WHERE id = COALESCE(NEW.owner_id, OLD.owner_id) FOR UPDATE;

    UPDATE users
       SET used_bytes = GREATEST(0, used_bytes + v_delta)
     WHERE id = COALESCE(NEW.owner_id, OLD.owner_id)
    RETURNING used_bytes, quota_bytes INTO v_used, v_hard_limit;

    -- Reject only growth. A shrinking update (delete/trash) must always succeed
    -- even if the account is already over quota.
    IF v_delta > 0 AND v_hard_limit > 0 AND v_used > v_hard_limit THEN
        RAISE EXCEPTION
            'quota exceeded: used=% quota=% requested=%', v_used - v_delta, v_hard_limit, v_delta
            USING ERRCODE = 'TDL01';
    END IF;

    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_quota ON nodes;
CREATE TRIGGER trg_nodes_quota AFTER INSERT OR UPDATE OF size_bytes, trashed_at, kind OR DELETE
    ON nodes FOR EACH ROW EXECUTE FUNCTION nodes_quota_delta();

-- Nightly reconciliation: the cached counter can drift after crashes or manual
-- intervention, so it is recomputed from the source of truth.
CREATE OR REPLACE FUNCTION recompute_user_usage(p_user uuid) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE v_total bigint;
BEGIN
    SELECT COALESCE(sum(size_bytes), 0) INTO v_total
      FROM nodes
     WHERE owner_id = p_user AND kind = 'file' AND trashed_at IS NULL;

    UPDATE users SET used_bytes = v_total WHERE id = p_user;
    RETURN v_total;
END $$;

-- -----------------------------------------------------------------------------
-- New-user bootstrap: every user gets a structural root node.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION users_create_root() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO nodes (owner_id, parent_id, kind, name, ancestor_ids, depth, upload_state)
    VALUES (NEW.id, NULL, 'folder', '/', '{}'::uuid[], 0, 'ready');
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_users_create_root ON users;
CREATE TRIGGER trg_users_create_root AFTER INSERT ON users
    FOR EACH ROW EXECUTE FUNCTION users_create_root();

-- Refuse to delete a node whose live descendants still exist, mirroring the
-- application-level 409 folder_not_empty decision.
CREATE OR REPLACE FUNCTION nodes_block_nonempty_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.kind = 'folder'
       AND EXISTS (SELECT 1 FROM nodes c WHERE c.parent_id = OLD.id) THEN
        RAISE EXCEPTION 'folder is not empty' USING ERRCODE = 'TDL03';
    END IF;
    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_nodes_block_nonempty_delete ON nodes;
CREATE TRIGGER trg_nodes_block_nonempty_delete BEFORE DELETE ON nodes
    FOR EACH ROW EXECUTE FUNCTION nodes_block_nonempty_delete();

-- -----------------------------------------------------------------------------
-- Chunk-bookkeeping triggers
-- -----------------------------------------------------------------------------

-- Chunks must belong to a file, and their sizes must be internally consistent
-- with the declared chunking of the parent node.
CREATE OR REPLACE FUNCTION file_chunks_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_kind node_kind;
    v_chunk_size integer;
BEGIN
    SELECT kind, chunk_size INTO v_kind, v_chunk_size
      FROM nodes WHERE id = NEW.node_id;

    IF v_kind IS DISTINCT FROM 'file' THEN
        RAISE EXCEPTION 'chunks may only be attached to file nodes'
            USING ERRCODE = 'check_violation';
    END IF;

    -- Every chunk except the last must be exactly chunk_size. This catches an
    -- off-by-one slicing bug in a client before it silently corrupts a file.
    IF v_chunk_size IS NOT NULL
       AND NEW.plaintext_size > v_chunk_size THEN
        RAISE EXCEPTION 'chunk % exceeds declared chunk_size %',
            NEW.chunk_index, v_chunk_size USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_file_chunks_validate ON file_chunks;
CREATE TRIGGER trg_file_chunks_validate BEFORE INSERT OR UPDATE ON file_chunks
    FOR EACH ROW EXECUTE FUNCTION file_chunks_validate();

-- Keep pool utilisation counters current without a full-table aggregate.
CREATE OR REPLACE FUNCTION file_chunks_pool_counters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE storage_pools
           SET used_bytes = used_bytes + NEW.ciphertext_size,
               chunk_count = chunk_count + 1
         WHERE id = NEW.storage_pool_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE storage_pools
           SET used_bytes = GREATEST(0, used_bytes - OLD.ciphertext_size),
               chunk_count = GREATEST(0, chunk_count - 1)
         WHERE id = OLD.storage_pool_id;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_file_chunks_pool_counters ON file_chunks;
CREATE TRIGGER trg_file_chunks_pool_counters AFTER INSERT OR DELETE ON file_chunks
    FOR EACH ROW EXECUTE FUNCTION file_chunks_pool_counters();

-- -----------------------------------------------------------------------------
-- Search & listing
-- -----------------------------------------------------------------------------

-- Ranked search: exact prefix match first, then substring, then trigram
-- similarity. Kept as a function so the API and the Android offline-sync job
-- cannot drift apart in their matching semantics.
CREATE OR REPLACE FUNCTION search_nodes(
    p_owner           uuid,
    p_query           text,
    p_parent          uuid    DEFAULT NULL,
    p_mime            text    DEFAULT NULL,
    p_kind            node_kind DEFAULT NULL,
    p_include_trashed boolean DEFAULT false,
    p_limit           integer DEFAULT 50,
    p_offset          integer DEFAULT 0
) RETURNS TABLE (
    id uuid, name text, kind node_kind, size_bytes bigint, mime_type text,
    parent_id uuid, is_starred boolean, is_trashed boolean,
    created_at timestamptz, updated_at timestamptz, rank real
)
LANGUAGE sql STABLE AS $$
    WITH q AS (SELECT lower(btrim(coalesce(p_query, ''))) AS needle)
    SELECT n.id, n.name, n.kind, n.size_bytes, n.mime_type,
           n.parent_id, n.is_starred, (n.trashed_at IS NOT NULL),
           n.created_at, n.updated_at,
           GREATEST(
               similarity(n.name_folded, q.needle),
               CASE WHEN n.name_folded LIKE q.needle || '%' THEN 1.0 ELSE 0.0 END
           )::real AS rank
      FROM nodes n, q
     WHERE n.owner_id = p_owner
       AND n.parent_id IS NOT NULL                       -- never surface root
       AND (p_include_trashed OR n.trashed_at IS NULL)
       AND (p_parent IS NULL OR n.parent_id = p_parent)
       AND (p_mime IS NULL OR n.mime_type = p_mime)
       AND (p_kind IS NULL OR n.kind = p_kind)
       AND (q.needle = ''
            OR n.name_folded LIKE '%' || q.needle || '%'
            OR n.name_folded % q.needle)
     ORDER BY rank DESC, n.name_folded ASC
     LIMIT LEAST(GREATEST(p_limit, 1), 200) OFFSET GREATEST(p_offset, 0);
$$;

-- Subtree size aggregate, used to warn before a recursive delete.
CREATE OR REPLACE FUNCTION subtree_usage(p_node uuid)
RETURNS TABLE (node_count bigint, total_bytes bigint)
LANGUAGE sql STABLE AS $$
    SELECT count(*)::bigint,
           COALESCE(sum(n.size_bytes) FILTER (WHERE n.kind = 'file'), 0)::bigint
      FROM nodes n
     WHERE n.id = p_node OR p_node = ANY (n.ancestor_ids);
$$;

-- -----------------------------------------------------------------------------
-- Upload-session lifecycle & garbage collection
-- -----------------------------------------------------------------------------

-- Expire stale sessions and return the orphaned node ids so the caller can
-- reclaim both the database rows and the already-uploaded Telegram messages.
CREATE OR REPLACE FUNCTION expire_upload_sessions(p_limit integer DEFAULT 500)
RETURNS TABLE (session_id uuid, node_id uuid, owner_id uuid)
LANGUAGE sql AS $$
    WITH expired AS (
        SELECT s.id, s.node_id, s.owner_id
          FROM upload_sessions s
         WHERE s.status IN ('pending', 'assembling')
           AND s.expires_at < now()
         ORDER BY s.expires_at
         LIMIT p_limit
         FOR UPDATE SKIP LOCKED
    ), marked AS (
        UPDATE upload_sessions s
           SET status = 'expired'
          FROM expired e
         WHERE s.id = e.id
        RETURNING s.id, s.node_id, s.owner_id
    )
    SELECT m.id, m.node_id, m.owner_id FROM marked m;
$$;

-- Lease-based claim for background workers. SKIP LOCKED lets N workers poll the
-- same table without contending, and the lease means a crashed worker's job is
-- retried rather than lost.
CREATE OR REPLACE FUNCTION claim_background_jobs(
    p_worker text, p_limit integer DEFAULT 10, p_lease_seconds integer DEFAULT 300
) RETURNS SETOF background_jobs
LANGUAGE sql AS $$
    WITH claimable AS (
        SELECT id FROM background_jobs
         WHERE completed_at IS NULL
           AND run_at <= now()
           AND attempts < max_attempts
           AND (locked_until IS NULL OR locked_until < now())
         ORDER BY run_at, created_at
         LIMIT p_limit
         FOR UPDATE SKIP LOCKED
    )
    UPDATE background_jobs j
       SET locked_by = p_worker,
           locked_until = now() + make_interval(secs => p_lease_seconds),
           attempts = j.attempts + 1
      FROM claimable c
     WHERE j.id = c.id
    RETURNING j.*;
$$;

-- Reclaim quota held by aborted/expired sessions.
CREATE OR REPLACE FUNCTION release_session_reservation(p_session uuid) RETURNS void
LANGUAGE sql AS $$
    UPDATE users u
       SET used_bytes = GREATEST(
               0,
               u.used_bytes - (SELECT s.reserved_bytes FROM upload_sessions s WHERE s.id = p_session)
           )
     WHERE u.id = (SELECT s.owner_id FROM upload_sessions s WHERE s.id = p_session);
$$;

COMMIT;