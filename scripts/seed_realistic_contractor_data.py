"""Seed rich, realistic 6-month financial history for Colombian independent contractor.

Covers May 1, 2026 to October 4, 2026:
- Monthly fixed & big expenses: Rent on 1st, Big Grocery market on 2nd/3rd (with receipt items),
  mid-month replenishment market on 16th, utilities on 12-18th, subscriptions on 10, 25, 28th.
- Weekday small expenses: lunches, coffee, transport.
- Weekend expenses: restaurants, entertainment.
- Contractor incomes: regular retainer on 1st & 15th, plus occasional freelance.

Executes in a SINGLE transaction per user for speed and connection resilience.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal

# Set Neon DB URL before importing settings
os.environ["FINANCE_AI_DATABASE_URL"] = (
    "postgresql+psycopg://neondb_owner:npg_pcxN62GAHhZW@ep-withered-rain-acg9wncp.sa-east-1.aws.neon.tech/neondb?sslmode=require"
)

from sqlalchemy import text

from finance_ai.accounts.dependencies import get_account_service
from finance_ai.accounts.schemas import AccountCreateCommand, AccountType
from finance_ai.categories.dependencies import get_category_service
from finance_ai.categories.schemas import CategoryColor, CategoryCreateCommand, CategoryIcon
from finance_ai.db.models import AccountMovement, Expense, FinancialTransaction, Income, Receipt
from finance_ai.db.session import get_session_factory


def seed_user(email: str):
    session_factory = get_session_factory()

    with session_factory() as session:
        user_row = session.execute(
            text("SELECT id, email, preferred_currency_id FROM app_user WHERE email = :email"),
            {"email": email},
        ).fetchone()
        if not user_row:
            print(f"User {email} not found in database! Skipping.")
            return
        user_id = user_row[0]
        currency_id = user_row[2] or 1
        print("\n========================================================")
        print(f"Seeding rich 6-month history for {email} (id={user_id})...")
        print("========================================================")

    # 1. Categories
    category_service = get_category_service()
    existing_cats = {c.name.lower(): c for c in category_service.list(owner_id=user_id).categories}

    categories_to_ensure = [
        ("Supermercado", CategoryColor.GREEN, CategoryIcon.FOOD),
        ("Restaurantes", CategoryColor.ORANGE, CategoryIcon.FOOD),
        ("Transporte", CategoryColor.BLUE, CategoryIcon.TRANSPORT),
        ("Servicios", CategoryColor.TEAL, CategoryIcon.BILLS),
        ("Salud y Farmacia", CategoryColor.RED, CategoryIcon.HEALTH),
        ("Entretenimiento", CategoryColor.PURPLE, CategoryIcon.ENTERTAINMENT),
        ("Subscripción", CategoryColor.GRAY, CategoryIcon.WANTS),
        ("Hogar", CategoryColor.YELLOW, CategoryIcon.HOME),
    ]

    cat_map = {}
    for name, color, icon in categories_to_ensure:
        if name.lower() in existing_cats:
            cat_map[name] = existing_cats[name.lower()].id
        else:
            created_cat = category_service.create(
                owner_id=user_id,
                command=CategoryCreateCommand(name=name, color=color, icon=icon),
            )
            cat_map[name] = created_cat.id
            print(f"  + Category created: {name} (id={created_cat.id})")

    # Refresh categories
    all_cats = category_service.list(owner_id=user_id).categories
    for c in all_cats:
        cat_map[c.name] = c.id

    # 2. Accounts
    account_service = get_account_service()
    existing_accs = {
        a.name.lower(): a
        for a in account_service.list(
            owner_id=user_id, preferred_currency_code="COP", include_inactive=True
        ).accounts
    }

    accounts_to_ensure = [
        ("Bancolombia Ahorros", AccountType.BANK, Decimal("3500000.00")),
        ("Nequi", AccountType.DIGITAL_WALLET, Decimal("650000.00")),
        ("Efectivo", AccountType.CASH, Decimal("200000.00")),
    ]

    acc_map = {}
    for name, acc_type, opening in accounts_to_ensure:
        if name.lower() in existing_accs:
            acc_map[name] = existing_accs[name.lower()].id
        else:
            alt_name = "bancolombia" if "bancolombia" in name.lower() else name.lower()
            matched = False
            for ex_n, ex_a in existing_accs.items():
                if alt_name in ex_n:
                    acc_map[name] = ex_a.id
                    matched = True
                    break
            if not matched:
                created_acc = account_service.create(
                    owner_id=user_id,
                    command=AccountCreateCommand(
                        name=name,
                        account_type=acc_type,
                        currency_code="COP",
                        opening_balance=opening,
                    ),
                )
                acc_map[name] = created_acc.id
                print(f"  + Account created: {name} (id={created_acc.id})")

    bancolombia_id = acc_map.get("Bancolombia Ahorros") or acc_map.get("Bancolombia")
    nequi_id = acc_map.get("Nequi")
    efectivo_id = acc_map.get("Efectivo") or nequi_id

    # 3. Income Sources (from incomesource catalog: 1=Salary, 2=Freelance, 3=Investment, 4=Other)
    with session_factory() as session:
        inc_sources = session.execute(text("SELECT id, name FROM incomesource")).fetchall()
        source_by_name = {r[1].lower(): r[0] for r in inc_sources}

    salary_id = source_by_name.get("salary", 1)
    freelance_id = source_by_name.get("freelance", 2)
    investment_id = source_by_name.get("investment", 3)

    source_map = {
        "Honorarios Consultoría": salary_id,
        "Diseño & Desarrollo Web": freelance_id,
        "Rendimientos CDT": investment_id,
    }

    # 4. Clean old transactions for user and insert all in a SINGLE transaction
    print("  -> Populating consistent 6-month ledger in single fast transaction...")
    with session_factory() as session:
        # Delete old
        session.execute(text("DELETE FROM account_movement WHERE user_id = :uid"), {"uid": user_id})
        session.execute(text("DELETE FROM expense WHERE user_id = :uid"), {"uid": user_id})
        session.execute(text("DELETE FROM receipt WHERE user_id = :uid"), {"uid": user_id})
        session.execute(
            text("DELETE FROM financial_transaction WHERE user_id = :uid"), {"uid": user_id}
        )
        session.execute(text("DELETE FROM income WHERE user_id = :uid"), {"uid": user_id})
        session.flush()

        months = [
            (2026, 5, 31),
            (2026, 6, 30),
            (2026, 7, 31),
            (2026, 8, 31),
            (2026, 9, 30),
            (2026, 10, 4),  # Current month: first 4 days
        ]

        total_incomes = 0
        total_expenses = 0

        def add_income(acc_id: int, amt: Decimal, d: date, desc: str, src_id: int):
            nonlocal total_incomes
            tx = FinancialTransaction(
                user_id=user_id,
                transaction_type="income",
                status="confirmed",
                occurred_at=datetime.combine(d, time(9, 0), UTC),
                description=desc,
                income_source_id=src_id,
                capture_source="manual",
            )
            session.add(tx)
            session.flush()

            mv = AccountMovement(
                user_id=user_id,
                transaction_id=tx.id,
                account_id=acc_id,
                amount=amt,
            )
            session.add(mv)

            inc = Income(
                user_id=user_id,
                income_source_id=src_id,
                currency_id=currency_id,
                amount=amt,
                date=d,
                description=desc,
            )
            session.add(inc)
            total_incomes += 1

        def add_expense(
            acc_id: int,
            amt: Decimal,
            merch: str,
            occ: datetime,
            c_id: int,
            note: str,
            items: list[tuple[str, Decimal, Decimal]] | None = None,
        ):
            nonlocal total_expenses
            tx = FinancialTransaction(
                user_id=user_id,
                transaction_type="expense",
                status="confirmed",
                occurred_at=occ,
                description=merch,
                capture_source="receipt_photo_ai" if items else "manual",
            )
            session.add(tx)
            session.flush()

            mv = AccountMovement(
                user_id=user_id,
                transaction_id=tx.id,
                account_id=acc_id,
                amount=-amt,
            )
            session.add(mv)

            rc = Receipt(
                user_id=user_id,
                transaction_id=tx.id,
                store=merch,
                date=occ.date(),
                currency_id=currency_id,
                category_id=c_id,
                purchase_reason=note,
                total=amt,
                payment_method_id=1,
            )
            session.add(rc)
            session.flush()

            if items:
                for idx, (it_name, it_qty, it_price) in enumerate(items, start=1):
                    ex = Expense(
                        user_id=user_id,
                        receipt_id=rc.id,
                        category_id=c_id,
                        position=idx,
                        name=it_name,
                        amount=it_qty * it_price,
                        quantity=it_qty,
                        unit_price=it_price,
                        note=note,
                    )
                    session.add(ex)
            else:
                ex = Expense(
                    user_id=user_id,
                    receipt_id=rc.id,
                    category_id=c_id,
                    position=1,
                    name=merch,
                    amount=amt,
                    quantity=Decimal("1.0"),
                    unit_price=amt,
                    note=note,
                )
                session.add(ex)
            total_expenses += 1

        for year, month, max_day in months:
            # 1. Monthly Retainer Income on 1st ($4.200.000 COP)
            add_income(
                acc_id=bancolombia_id,
                amt=Decimal("4200000.00"),
                d=date(year, month, 1),
                desc=f"Honorarios Consultoría Mes {month:02d}/{year}",
                src_id=source_map["Honorarios Consultoría"],
            )

            # 2. Quincenal Retainer on 15th ($1.800.000 COP)
            if max_day >= 15:
                add_income(
                    acc_id=bancolombia_id,
                    amt=Decimal("1800000.00"),
                    d=date(year, month, 15),
                    desc=f"Honorarios Quincenales Cliente B {month:02d}/{year}",
                    src_id=source_map["Honorarios Consultoría"],
                )

            # 3. Extra Freelance Project in June and August ($1.250.000 COP)
            if month in (6, 8) and max_day >= 20:
                add_income(
                    acc_id=nequi_id,
                    amt=Decimal("1250000.00"),
                    d=date(year, month, 20),
                    desc=f"Desarrollo Frontend Extra {month:02d}/{year}",
                    src_id=source_map["Diseño & Desarrollo Web"],
                )

            # 4. Rent on Day 1 ($1.350.000 COP)
            add_expense(
                acc_id=bancolombia_id,
                amt=Decimal("1350000.00"),
                merch="Arriendo Apartamento",
                occ=datetime(year, month, 1, 9, 30, tzinfo=UTC),
                c_id=cat_map["Hogar"],
                note=f"Canon arriendo mes {month:02d}",
            )

            # 5. Big Monthly Supermarket on Day 2 with receipt items (~$550.000 COP)
            if max_day >= 2:
                items_m = [
                    ("Arroz Diana Premium 5kg", Decimal("1.0"), Decimal("24500.00")),
                    ("Aceite Premier Girasol 3L", Decimal("1.0"), Decimal("38900.00")),
                    ("Pechuga de Pollo Fresca 2kg", Decimal("2.0"), Decimal("18500.00")),
                    ("Carne de Res Pulpa 1.5kg", Decimal("1.5"), Decimal("32000.00")),
                    ("Huevos Santa Anita AA x30", Decimal("2.0"), Decimal("21800.00")),
                    ("Leche Alquería Entera 1.1L (x6)", Decimal("6.0"), Decimal("5600.00")),
                    ("Café Juan Valdez Colina 454g", Decimal("1.0"), Decimal("34500.00")),
                    ("Frutas y Verduras Frescas Surtidas", Decimal("1.0"), Decimal("68400.00")),
                    ("Detergente Ariel Power Líquido 3L", Decimal("1.0"), Decimal("42900.00")),
                    ("Papel Higiénico Familia Acolchomax x12", Decimal("1.0"), Decimal("28500.00")),
                ]
                sum_m = sum(q * p for _, q, p in items_m)
                add_expense(
                    acc_id=bancolombia_id,
                    amt=sum_m,
                    merch="Éxito Calle 80",
                    occ=datetime(year, month, 2, 11, 15, tzinfo=UTC),
                    c_id=cat_map["Supermercado"],
                    note=f"Gran mercado mensual mes {month:02d}",
                    items=items_m,
                )

            # 6. Netflix on Day 10 ($44.900 COP)
            if max_day >= 10:
                add_expense(
                    acc_id=nequi_id,
                    amt=Decimal("44900.00"),
                    merch="Netflix",
                    occ=datetime(year, month, 10, 8, 0, tzinfo=UTC),
                    c_id=cat_map["Subscripción"],
                    note="Suscripción mensual Netflix 4K",
                )

            # 7. Enel Energía on Day 12 ($145.000 COP)
            if max_day >= 12:
                add_expense(
                    acc_id=bancolombia_id,
                    amt=Decimal("142000.00") + Decimal(month * 1000),
                    merch="Enel Colombia",
                    occ=datetime(year, month, 12, 14, 0, tzinfo=UTC),
                    c_id=cat_map["Servicios"],
                    note="Factura energía eléctrica",
                )

            # 8. Acueducto on Day 14 ($78.500 COP)
            if max_day >= 14:
                add_expense(
                    acc_id=bancolombia_id,
                    amt=Decimal("78500.00"),
                    merch="Acueducto de Bogotá",
                    occ=datetime(year, month, 14, 15, 0, tzinfo=UTC),
                    c_id=cat_map["Servicios"],
                    note="Factura agua y alcantarillado",
                )

            # 9. Vanti Gas on Day 16 ($31.500 COP) & D1 Replenishment Market (~$95.000 COP)
            if max_day >= 16:
                add_expense(
                    acc_id=bancolombia_id,
                    amt=Decimal("31500.00"),
                    merch="Gas Natural Vanti",
                    occ=datetime(year, month, 16, 10, 0, tzinfo=UTC),
                    c_id=cat_map["Servicios"],
                    note="Factura gas natural",
                )
                items_d1 = [
                    ("Pan Tajado Bimbo Artesano", Decimal("1.0"), Decimal("10500.00")),
                    ("Queso Campesino Tajado 500g", Decimal("1.0"), Decimal("16500.00")),
                    ("Jamón Pietrán de Pierna 450g", Decimal("1.0"), Decimal("19500.00")),
                    ("Yogurt Griego Natural Pasco x4", Decimal("1.0"), Decimal("18000.00")),
                    ("Frutas de Temporada", Decimal("1.0"), Decimal("24500.00")),
                ]
                sum_d1 = sum(q * p for _, q, p in items_d1)
                add_expense(
                    acc_id=nequi_id,
                    amt=sum_d1,
                    merch="Tiendas D1",
                    occ=datetime(year, month, 16, 17, 30, tzinfo=UTC),
                    c_id=cat_map["Supermercado"],
                    note=f"Mercado quincenal reposición mes {month:02d}",
                    items=items_d1,
                )

            # 10. Claro Internet on Day 18 ($94.900 COP)
            if max_day >= 18:
                add_expense(
                    acc_id=bancolombia_id,
                    amt=Decimal("94900.00"),
                    merch="Claro Hogar",
                    occ=datetime(year, month, 18, 11, 0, tzinfo=UTC),
                    c_id=cat_map["Servicios"],
                    note="Fibra óptica 500MB + Telefonía",
                )

            # 11. Spotify on Day 25 ($34.900 COP)
            if max_day >= 25:
                add_expense(
                    acc_id=nequi_id,
                    amt=Decimal("34900.00"),
                    merch="Spotify",
                    occ=datetime(year, month, 25, 8, 30, tzinfo=UTC),
                    c_id=cat_map["Subscripción"],
                    note="Suscripción mensual Spotify Familiar",
                )

            # 12. ChatGPT Plus on Day 28 ($85.000 COP)
            if max_day >= 28:
                add_expense(
                    acc_id=nequi_id,
                    amt=Decimal("85000.00"),
                    merch="OpenAI",
                    occ=datetime(year, month, 28, 9, 0, tzinfo=UTC),
                    c_id=cat_map["Subscripción"],
                    note="ChatGPT Plus desarrollo IA",
                )

            # Regular Day-to-Day Weekday & Weekend Expenses
            for d in range(1, max_day + 1):
                dt = date(year, month, d)
                weekday = dt.weekday()

                if d > 2:
                    # Lunch corrientazo (Tuesday & Thursday)
                    if weekday in (1, 3):
                        add_expense(
                            acc_id=nequi_id,
                            amt=Decimal("19500.00"),
                            merch="Restaurante Don Pedro",
                            occ=datetime(year, month, d, 13, 10, tzinfo=UTC),
                            c_id=cat_map["Restaurantes"],
                            note="Almuerzo ejecutivo menú del día",
                        )

                    # Coffee / snack (Mon, Wed, Fri)
                    if weekday in (0, 2, 4):
                        add_expense(
                            acc_id=efectivo_id,
                            amt=Decimal("9500.00"),
                            merch="Café Juan Valdez",
                            occ=datetime(year, month, d, 16, 20, tzinfo=UTC),
                            c_id=cat_map["Restaurantes"],
                            note="Capuchino y pan de bono",
                        )

                    # Transport (Mon, Thu)
                    if weekday in (0, 3):
                        add_expense(
                            acc_id=nequi_id,
                            amt=Decimal("18500.00"),
                            merch="Taxi Urbano",
                            occ=datetime(year, month, d, 18, 45, tzinfo=UTC),
                            c_id=cat_map["Transporte"],
                            note="Transporte regreso reunión",
                        )

                # Gasoline on 8th and 22nd
                if d in (8, 22) and d <= max_day:
                    add_expense(
                        acc_id=bancolombia_id,
                        amt=Decimal("98000.00"),
                        merch="Estación Terpel Autopista",
                        occ=datetime(year, month, d, 8, 15, tzinfo=UTC),
                        c_id=cat_map["Transporte"],
                        note="Tanqueada de combustible",
                    )

                # Pharmacy on 19th
                if d == 19 and d <= max_day:
                    add_expense(
                        acc_id=nequi_id,
                        amt=Decimal("38500.00"),
                        merch="Farmacia Cruz Verde",
                        occ=datetime(year, month, d, 19, 0, tzinfo=UTC),
                        c_id=cat_map["Salud y Farmacia"],
                        note="Vitaminas y cuidado personal",
                    )

                # Saturday dinner out
                if weekday == 5 and d <= max_day and d > 3:
                    add_expense(
                        acc_id=bancolombia_id,
                        amt=Decimal("84500.00"),
                        merch="Crepes & Waffles Zona T",
                        occ=datetime(year, month, d, 20, 15, tzinfo=UTC),
                        c_id=cat_map["Restaurantes"],
                        note="Cena sábado familiar",
                    )

                # Sunday movie on 2nd Sunday
                if weekday == 6 and 7 <= d <= 14 and d <= max_day:
                    add_expense(
                        acc_id=bancolombia_id,
                        amt=Decimal("62000.00"),
                        merch="Cine Colombia Titán Plaza",
                        occ=datetime(year, month, d, 16, 0, tzinfo=UTC),
                        c_id=cat_map["Entretenimiento"],
                        note="Entradas cine 2D + combo",
                    )

        # Commit entire 6 months in ONE single transaction
        session.commit()

        print(f"\n✅ Successfully committed entire 6-month history for {email}!")
        print(f"   Incomes created: {total_incomes}")
        print(f"   Expenses created: {total_expenses}")


def main():
    target_emails = ["ivangiraldav2016@gmail.com", "test@gmail.com"]
    if len(sys.argv) > 1:
        target_emails = [sys.argv[1]]

    for email in target_emails:
        seed_user(email)


if __name__ == "__main__":
    main()
