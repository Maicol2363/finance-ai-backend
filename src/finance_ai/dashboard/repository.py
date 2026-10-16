from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class DashboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def validate_currency(self, currency_code: str) -> bool:
        stmt = text("SELECT 1 FROM currency WHERE code = :code AND is_active = true")
        result = self.session.execute(stmt, {"code": currency_code}).scalar()
        return result is not None

    def get_flow_totals(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        income_stmt = text(
            """
            SELECT
              COALESCE((
                SELECT SUM(i.amount)
                FROM income i
                JOIN currency c ON c.id = i.currency_id
                WHERE i.user_id = :user_id
                  AND c.code = :currency
                  AND i.date BETWEEN :start AND :end
              ), 0)
              + COALESCE((
                SELECT SUM(am.amount)
                FROM financial_transaction ft
                JOIN account_movement am
                  ON am.transaction_id = ft.id AND am.user_id = ft.user_id
                JOIN account a ON a.id = am.account_id AND a.user_id = am.user_id
                JOIN currency c ON c.id = a.currency_id
                WHERE ft.user_id = :user_id
                  AND ft.type = 'income'
                  AND ft.status = 'confirmed'
                  AND am.amount > 0
                  AND c.code = :currency
                  AND (ft.occurred_at AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
              ), 0)
            """
        )
        total_incomes = self.session.execute(
            income_stmt,
            {"user_id": user_id, "currency": currency, "start": start_date, "end": end_date},
        ).scalar() or Decimal("0")

        expense_stmt = text(
            """
            SELECT COALESCE(SUM(-am.amount), 0)
            FROM financial_transaction ft
            JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
            JOIN account a ON a.id = am.account_id
            JOIN currency c ON c.id = a.currency_id
            WHERE ft.user_id = :user_id
              AND ft.type = 'expense'
              AND ft.status <> 'voided'
              AND am.amount < 0
              AND c.code = :currency
              AND (ft.occurred_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
            """
        )
        total_expenses = self.session.execute(
            expense_stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "start": start_date,
                "end": end_date,
            },
        ).scalar() or Decimal("0")

        return Decimal(total_incomes), Decimal(total_expenses)

    def get_period_expenses(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> Decimal:
        stmt = text(
            """
            SELECT COALESCE(SUM(-am.amount), 0)
            FROM financial_transaction ft
            JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
            JOIN account a ON a.id = am.account_id
            JOIN currency c ON c.id = a.currency_id
            WHERE ft.user_id = :user_id
              AND ft.type = 'expense'
              AND ft.status <> 'voided'
              AND am.amount < 0
              AND c.code = :currency
              AND (ft.occurred_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
            """
        )
        res = self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "start": start_date,
                "end": end_date,
            },
        ).scalar()
        return Decimal(res or 0)

    def get_weekly_spend(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> list[dict[str, Any]]:
        stmt = text(
            """
            WITH bounds AS (
              SELECT gs::date AS wk_start
              FROM generate_series(
                date_trunc('week', CAST(:start AS date)), CAST(:end AS date), interval '7 days'
              ) gs
            ), weeks AS (
              SELECT GREATEST(wk_start, CAST(:start AS date)) AS w_start,
                     LEAST(wk_start + 6, CAST(:end AS date)) AS w_end
              FROM bounds
            ), spend AS (
              SELECT (ft.occurred_at AT TIME ZONE :tz)::date AS day,
                     -am.amount AS amount
              FROM financial_transaction ft
              JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
              JOIN account a ON a.id = am.account_id
              JOIN currency c ON c.id = a.currency_id
              WHERE ft.user_id = :user_id
                AND ft.type = 'expense'
                AND ft.status <> 'voided'
                AND am.amount < 0
                AND c.code = :currency
                AND (ft.occurred_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
            )
            SELECT w.w_start, w.w_end, COALESCE(SUM(s.amount), 0) AS expenses
            FROM weeks w
            LEFT JOIN spend s ON s.day BETWEEN w.w_start AND w.w_end
            GROUP BY w.w_start, w.w_end
            ORDER BY w.w_start;
            """
        )
        rows = self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "start": start_date,
                "end": end_date,
            },
        ).all()
        return [
            {
                "start": row[0],
                "end": row[1],
                "expenses": Decimal(row[2]),
            }
            for row in rows
        ]

    def get_top_categories(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        stmt = text(
            """
            SELECT COALESCE(e.category_id, r.category_id) AS category_id,
                   COALESCE(cat.name, 'Sin categoría') AS name,
                   SUM(e.amount) AS total
            FROM receipt r
            JOIN financial_transaction ft ON ft.id = r.transaction_id
            JOIN expense e ON e.receipt_id = r.id
            JOIN currency c ON c.id = r.currency_id
            LEFT JOIN category cat ON cat.id = COALESCE(e.category_id, r.category_id)
            WHERE r.user_id = :user_id
              AND ft.type = 'expense'
              AND ft.status <> 'voided'
              AND c.code = :currency
              AND (ft.occurred_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
            GROUP BY 1, 2
            ORDER BY total DESC
            LIMIT :limit;
            """
        )
        rows = self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "start": start_date,
                "end": end_date,
                "limit": limit,
            },
        ).all()
        return [
            {
                "category_id": row[0],
                "name": row[1],
                "total": Decimal(row[2]),
            }
            for row in rows
        ]

    def get_projection_data(
        self,
        *,
        user_id: int,
        currency: str,
        month_start: date,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        stmt = text(
            """
            WITH spend AS (
              SELECT (ft.occurred_at AT TIME ZONE :tz)::date AS day,
                     -am.amount AS amount
              FROM financial_transaction ft
              JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
              JOIN account a ON a.id = am.account_id
              JOIN currency c ON c.id = a.currency_id
              WHERE ft.user_id = :user_id
                AND ft.type = 'expense'
                AND ft.status <> 'voided'
                AND am.amount < 0
                AND c.code = :currency
            )
            SELECT
              COALESCE(
                SUM(amount) FILTER (WHERE day BETWEEN :month_start AND :today), 0
              ) AS spent_mtd,
              COALESCE(
                SUM(amount) FILTER (WHERE day BETWEEN :month_start - 90 AND :month_start - 1), 0
              ) / 90.0 AS hist_daily
            FROM spend;
            """
        )
        row = self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "month_start": month_start,
                "today": today,
            },
        ).one()
        return Decimal(row[0] or 0), Decimal(row[1] or 0)

    def get_balance_and_runway(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        stmt = text(
            """
            WITH bal AS (
              SELECT COALESCE(SUM(a.opening_balance + COALESCE(m.total, 0)), 0) AS balance
              FROM account a
              JOIN currency c ON c.id = a.currency_id
              LEFT JOIN (
                SELECT account_id, SUM(amount) AS total
                FROM account_movement
                WHERE user_id = :user_id
                GROUP BY account_id
              ) m ON m.account_id = a.id
              WHERE a.user_id = :user_id
                AND a.is_active = true
                AND c.code = :currency
            ), spend AS (
              SELECT (ft.occurred_at AT TIME ZONE :tz)::date AS day,
                     -am.amount AS amount
              FROM financial_transaction ft
              JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
              JOIN account a ON a.id = am.account_id
              JOIN currency c ON c.id = a.currency_id
              WHERE ft.user_id = :user_id
                AND ft.type = 'expense'
                AND ft.status <> 'voided'
                AND am.amount < 0
                AND c.code = :currency
            ), monthly AS (
              SELECT date_trunc('month', day) AS m, SUM(amount) AS spent
              FROM spend
              WHERE day >= (date_trunc('month', CAST(:today AS date)) - interval '3 months')::date
                AND day < date_trunc('month', CAST(:today AS date))::date
              GROUP BY 1
            )
            SELECT
              bal.balance,
              COALESCE(SUM(monthly.spent) / 3.0, 0) AS avg_monthly_spend
            FROM bal LEFT JOIN monthly ON true
            GROUP BY bal.balance;
            """
        )
        row = self.session.execute(
            stmt,
            {"user_id": user_id, "currency": currency, "today": today, "tz": tz},
        ).one()
        balance = Decimal(row[0] or 0)
        avg_monthly_spend = Decimal(row[1] or 0)
        runway_months = (balance / avg_monthly_spend) if avg_monthly_spend > 0 else None
        return balance, avg_monthly_spend, runway_months

    def get_income_average(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        stmt = text(
            """
            WITH months AS (
              SELECT generate_series(
                date_trunc('month', CAST(:today AS date)) - interval '6 months',
                date_trunc('month', CAST(:today AS date)) - interval '1 month',
                interval '1 month'
              )::date AS m
            ), income_rows AS (
              SELECT i.date AS income_date, i.amount
              FROM income i
              JOIN currency c ON c.id = i.currency_id
              WHERE i.user_id = :user_id
                AND c.code = :currency
                AND i.date >=
                  (date_trunc('month', CAST(:today AS date)) - interval '6 months')::date
                AND i.date < date_trunc('month', CAST(:today AS date))::date
              UNION ALL
              SELECT (ft.occurred_at AT TIME ZONE 'UTC')::date AS income_date, am.amount
              FROM financial_transaction ft
              JOIN account_movement am
                ON am.transaction_id = ft.id AND am.user_id = ft.user_id
              JOIN account a ON a.id = am.account_id AND a.user_id = am.user_id
              JOIN currency c ON c.id = a.currency_id
              WHERE ft.user_id = :user_id
                AND ft.type = 'income'
                AND ft.status = 'confirmed'
                AND am.amount > 0
                AND c.code = :currency
                AND (ft.occurred_at AT TIME ZONE 'UTC')::date >=
                  (date_trunc('month', CAST(:today AS date)) - interval '6 months')::date
                AND (ft.occurred_at AT TIME ZONE 'UTC')::date
                  < date_trunc('month', CAST(:today AS date))::date
            ), inc AS (
              SELECT date_trunc('month', income_date)::date AS m, SUM(amount) AS total
              FROM income_rows
              GROUP BY 1
            )
            SELECT
              COALESCE(AVG(COALESCE(inc.total, 0)) FILTER (
                WHERE months.m >= date_trunc('month', CAST(:today AS date)) - interval '3 months'
              ), 0) AS avg_3m,
              COALESCE(AVG(COALESCE(inc.total, 0)), 0) AS avg_6m,
              STDDEV_POP(COALESCE(inc.total, 0)) AS stddev,
              AVG(COALESCE(inc.total, 0)) AS avg_inc
            FROM months
            LEFT JOIN inc USING (m);
            """
        )
        row = self.session.execute(
            stmt,
            {"user_id": user_id, "currency": currency, "today": today},
        ).one()
        avg_3m = Decimal(row[0] or 0)
        avg_6m = Decimal(row[1] or 0)
        stddev = Decimal(row[2] or 0) if row[2] is not None else Decimal("0")
        avg_inc = Decimal(row[3] or 0) if row[3] is not None else Decimal("0")
        variability = (stddev / avg_inc) if avg_inc > 0 else None
        return avg_3m, avg_6m, variability

    def get_weekday_pattern(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        stmt = text(
            """
            WITH days AS (
              SELECT d::date AS day
              FROM generate_series(
                CAST(:today AS date) - 89, CAST(:today AS date), interval '1 day'
              ) d
            ), spend AS (
              SELECT (ft.occurred_at AT TIME ZONE :tz)::date AS day,
                     -am.amount AS amount
              FROM financial_transaction ft
              JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
              JOIN account a ON a.id = am.account_id
              JOIN currency c ON c.id = a.currency_id
              WHERE ft.user_id = :user_id
                AND ft.type = 'expense'
                AND ft.status <> 'voided'
                AND am.amount < 0
                AND c.code = :currency
            ), per_day AS (
              SELECT days.day, COALESCE(SUM(s.amount), 0) AS spent
              FROM days
              LEFT JOIN spend s ON s.day = days.day
              GROUP BY days.day
            )
            SELECT
              COALESCE(
                AVG(spent) FILTER (WHERE EXTRACT(ISODOW FROM day) IN (6, 7)), 0
              ) AS weekend_avg,
              COALESCE(
                AVG(spent) FILTER (WHERE EXTRACT(ISODOW FROM day) < 6), 0
              ) AS weekday_avg
            FROM per_day;
            """
        )
        row = self.session.execute(
            stmt,
            {"user_id": user_id, "currency": currency, "today": today, "tz": tz},
        ).one()
        return Decimal(row[0] or 0), Decimal(row[1] or 0)

    def get_recurring_candidates(
        self,
        *,
        user_id: int,
        today: date,
        tz: str,
    ) -> list[dict[str, Any]]:
        stmt = text(
            """
            WITH tx AS (
              SELECT LOWER(TRIM(r.store)) AS merchant, r.total,
                     (ft.occurred_at AT TIME ZONE :tz)::date AS day
              FROM receipt r
              JOIN financial_transaction ft ON ft.id = r.transaction_id
              WHERE r.user_id = :user_id
                AND ft.type = 'expense'
                AND ft.status <> 'voided'
                AND ft.occurred_at >= now() - interval '6 months'
            ), gaps AS (
              SELECT merchant, total, day,
                     day - LAG(day) OVER (PARTITION BY merchant ORDER BY day) AS gap
              FROM tx
            ), series AS (
              SELECT merchant, COUNT(*) AS n, AVG(total) AS typical_amount, MAX(day) AS last_day,
                     PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap,
                     STDDEV_POP(total) / NULLIF(AVG(total), 0) AS amount_cv
              FROM gaps
              GROUP BY merchant
            )
            SELECT merchant, typical_amount, last_day + (median_gap::int) AS next_expected
            FROM series
            WHERE n >= 3
              AND median_gap BETWEEN 26 AND 35
              AND (amount_cv < 0.15 OR amount_cv IS NULL)
              AND last_day + (median_gap::int) BETWEEN :today AND :today + 30
            ORDER BY next_expected;
            """
        )
        rows = self.session.execute(
            stmt,
            {"user_id": user_id, "today": today, "tz": tz},
        ).all()
        return [
            {
                "merchant": str(row[0]),
                "typical_amount": Decimal(row[1]),
                "next_expected": row[2],
            }
            for row in rows
        ]

    def get_recent_movements(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        stmt = text(
            """
            SELECT ft.id, ft.type, ft.occurred_at,
                   COALESCE(r.store, ft.description, 'Sin detalle') AS title,
                   SUM(am.amount) AS amount
            FROM financial_transaction ft
            JOIN account_movement am ON am.transaction_id = ft.id AND am.user_id = ft.user_id
            JOIN account a ON a.id = am.account_id
            JOIN currency c ON c.id = a.currency_id
            LEFT JOIN receipt r ON r.transaction_id = ft.id
            WHERE ft.user_id = :user_id
              AND ft.status <> 'voided'
              AND ft.type IN ('expense', 'income')
              AND c.code = :currency
              AND (ft.occurred_at AT TIME ZONE :tz)::date BETWEEN :start AND :end
            GROUP BY ft.id, ft.type, ft.occurred_at, r.store, ft.description
            ORDER BY ft.occurred_at DESC, ft.id DESC
            LIMIT :limit;
            """
        )
        rows = self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "currency": currency,
                "tz": tz,
                "start": start_date,
                "end": end_date,
                "limit": limit,
            },
        ).all()
        return [
            {
                "id": row[0],
                "type": row[1],
                "occurred_at": row[2],
                "title": row[3],
                "amount": Decimal(row[4]),
            }
            for row in rows
        ]
