\set ON_ERROR_STOP on

SELECT format(
    'CREATE ROLE finance_ai_viewer LOGIN PASSWORD %L',
    :'viewer_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'finance_ai_viewer'
) \gexec

SELECT format(
    'ALTER ROLE finance_ai_viewer PASSWORD %L',
    :'viewer_password'
) \gexec

ALTER ROLE finance_ai_viewer SET default_transaction_read_only = on;
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO finance_ai_viewer',
    current_database()
) \gexec

REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM finance_ai_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE SELECT ON TABLES FROM finance_ai_viewer;

CREATE SCHEMA IF NOT EXISTS finance_ai_view;

CREATE OR REPLACE VIEW finance_ai_view.app_user AS
SELECT id, email, preferred_currency_id, is_active, created_at, updated_at
FROM public.app_user;

CREATE OR REPLACE VIEW finance_ai_view.currency AS
SELECT id, code, name, decimals, is_active
FROM public.currency;

CREATE OR REPLACE VIEW finance_ai_view.category AS
SELECT id, user_id, name, is_active, created_at, updated_at
FROM public.category;

CREATE OR REPLACE VIEW finance_ai_view.income_source AS
SELECT id, user_id, name, is_active, created_at, updated_at
FROM public.income_source;

CREATE OR REPLACE VIEW finance_ai_view.registration_summary AS
SELECT
    app_user.id AS user_id,
    app_user.email,
    currency.code AS preferred_currency,
    app_user.is_active,
    app_user.created_at,
    (SELECT count(*) FROM public.category WHERE category.user_id = app_user.id) AS categories,
    (SELECT count(*) FROM public.income_source WHERE income_source.user_id = app_user.id) AS income_sources
FROM public.app_user
JOIN public.currency ON currency.id = app_user.preferred_currency_id;

CREATE OR REPLACE VIEW finance_ai_view.user_session AS
SELECT
    id,
    user_id,
    platform,
    access_expires_at,
    idle_expires_at,
    absolute_expires_at,
    created_at,
    last_renewed_at,
    revoked_at,
    revocation_reason,
    (revoked_at IS NULL
        AND idle_expires_at > CURRENT_TIMESTAMP
        AND absolute_expires_at > CURRENT_TIMESTAMP) AS is_active
FROM public.user_session;

CREATE OR REPLACE VIEW finance_ai_view.account AS
SELECT
    id,
    user_id,
    name,
    type,
    currency_id,
    opening_balance,
    is_active,
    created_at,
    updated_at
FROM public.account;

CREATE OR REPLACE VIEW finance_ai_view.financial_transaction AS
SELECT id, user_id, type, status, occurred_at, capture_source, created_at, updated_at
FROM public.financial_transaction;

CREATE OR REPLACE VIEW finance_ai_view.account_movement AS
SELECT id, user_id, transaction_id, account_id, amount, created_at
FROM public.account_movement;

REVOKE ALL ON SCHEMA finance_ai_view FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA finance_ai_view FROM PUBLIC;
GRANT USAGE ON SCHEMA finance_ai_view TO finance_ai_viewer;
GRANT SELECT ON ALL TABLES IN SCHEMA finance_ai_view TO finance_ai_viewer;
