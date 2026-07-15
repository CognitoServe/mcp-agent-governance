-- app/schema.sql
-- All money columns use NUMERIC, never FLOAT/DOUBLE.
-- FLOAT/DOUBLE are binary fractions and cannot represent 0.10 exactly;
-- accumulated rounding errors make them unsafe for currency arithmetic.

-- ── agents ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT            PRIMARY KEY,
    name        TEXT            NOT NULL,
    cap         NUMERIC(12,2)   NOT NULL,
    spent       NUMERIC(12,2)   NOT NULL DEFAULT 0,
    reserved    NUMERIC(12,2)   NOT NULL DEFAULT 0,
    status      TEXT            NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ     NOT NULL,

    CONSTRAINT agents_cap_non_negative      CHECK (cap      >= 0),
    CONSTRAINT agents_spent_non_negative    CHECK (spent    >= 0),
    CONSTRAINT agents_reserved_non_negative CHECK (reserved >= 0),
    CONSTRAINT spent_reserved_within_cap    CHECK (spent + reserved <= cap)
);

-- Idempotently add the aggregate constraint to databases that pre-date it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'agents'
          AND constraint_name = 'spent_reserved_within_cap'
    ) THEN
        ALTER TABLE agents
            ADD CONSTRAINT spent_reserved_within_cap
            CHECK (spent + reserved <= cap);
    END IF;
END;
$$;

-- Seed record; idempotent via ON CONFLICT.
INSERT INTO agents (id, name, cap, created_at)
VALUES ('agent-1', 'Default Agent', 50.00, NOW())
ON CONFLICT (id) DO NOTHING;


-- ── agent_permissions ─────────────────────────────────────────────────────────
--   max_per_call = NULL   → tool is allowed with no per-call cost limit.
--   No row for (agent_id, tool_name) → tool is NOT permitted at all.
CREATE TABLE IF NOT EXISTS agent_permissions (
    agent_id     TEXT            NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    tool_name    TEXT            NOT NULL,
    max_per_call NUMERIC(12,2)   NULL,

    PRIMARY KEY (agent_id, tool_name),

    CONSTRAINT perm_max_per_call_non_negative
        CHECK (max_per_call IS NULL OR max_per_call >= 0)
);


-- ── audit_log ─────────────────────────────────────────────────────────────────
-- Hash-chained, tamper-evident ledger of every reserve/settle decision.
--
-- Each row's row_hash is sha256( prev_hash | agent_id | tool_name | decision |
--                                 reason | est_cost | actual_cost | ts.isoformat() )
-- so any retrospective edit to any field invalidates the hash of that row AND
-- every subsequent row (because the chain is broken at the tampered link).
--
-- The very first row uses prev_hash = '0' * 64 (genesis sentinel).
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL       PRIMARY KEY,
    agent_id    TEXT            NOT NULL,
    tool_name   TEXT            NOT NULL,
    decision    TEXT            NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason      TEXT            NOT NULL,
    est_cost    NUMERIC(12,2),
    actual_cost NUMERIC(12,2),
    ts          TIMESTAMPTZ     NOT NULL,
    prev_hash   TEXT            NOT NULL,
    row_hash    TEXT            NOT NULL
);


-- ── error_log ─────────────────────────────────────────────────────────────────
-- Technical error log for unexpected exceptions, separate from audit_log.
CREATE TABLE IF NOT EXISTS error_log (
    id          BIGSERIAL       PRIMARY KEY,
    source      TEXT            NOT NULL,
    error_type  TEXT            NOT NULL,
    message     TEXT            NOT NULL,
    context     JSONB,
    ts          TIMESTAMPTZ     NOT NULL DEFAULT now()
);


-- ── Demo agents for MCP server demos and Step 8 attacks ───────────────────────
--
-- demo-agent-analyst: read-heavy, no financial disbursement allowed.
--   Permitted: search (unlimited), write_record (max $5.00/call), cap=$100.
--   NOT permitted: disburse.
--
-- demo-agent-finance: full access including disbursement.
--   Permitted: search (unlimited), write_record (unlimited), disburse (max $50/call).
--   cap=$200.
INSERT INTO agents (id, name, cap, created_at)
VALUES
    ('demo-agent-analyst', 'Demo Analyst Agent', 100.00, NOW()),
    ('demo-agent-finance', 'Demo Finance Agent', 200.00, NOW())
ON CONFLICT (id) DO NOTHING;

INSERT INTO agent_permissions (agent_id, tool_name, max_per_call)
VALUES
    -- analyst permissions
    ('demo-agent-analyst', 'search',       NULL),    -- unlimited per-call
    ('demo-agent-analyst', 'write_record', 5.00),    -- max $5.00 per call

    -- finance permissions
    ('demo-agent-finance', 'search',       NULL),    -- unlimited per-call
    ('demo-agent-finance', 'write_record', NULL),    -- unlimited per-call
    ('demo-agent-finance', 'disburse',     50.00)    -- max $50.00 per call
    -- NOTE: demo-agent-analyst intentionally has NO disburse row → denied by governance
ON CONFLICT (agent_id, tool_name) DO UPDATE
    SET max_per_call = EXCLUDED.max_per_call;
