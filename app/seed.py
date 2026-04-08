from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting, Package, PaymentMethod, Sale, SaleItem, Service, User
from app.security import hash_password
from app.services.duplicates import build_suffix, current_month_bucket, extract_digits


def ensure_initial_admin(session: Session) -> User:
    admin_email = settings.initial_admin_email.strip().lower()
    admin_username = settings.initial_admin_username.strip() or admin_email.split("@", 1)[0] or "admin"

    admin = session.scalar(select(User).where(User.username == admin_username))
    if not admin:
        admin = session.scalar(select(User).where(func.lower(User.email) == admin_email))

    if not admin:
        admin = User(
            username=admin_username,
            full_name=settings.initial_admin_full_name,
            email=admin_email,
            password_hash=hash_password(settings.initial_admin_password),
            avatar_url=None,
            timezone_name=settings.initial_admin_timezone,
            subscription_ends_at=datetime.now(timezone.utc) + timedelta(days=3650),
            is_active=True,
            is_admin=True,
        )
        session.add(admin)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            admin = session.scalar(select(User).where(User.username == admin_username))
            if not admin:
                admin = session.scalar(select(User).where(func.lower(User.email) == admin_email))
            if not admin:
                raise
    else:
        admin.is_admin = True
        admin.is_active = True
        if not admin.username:
            admin.username = admin_username
        if not admin.email:
            admin.email = admin_email
        if not admin.full_name:
            admin.full_name = settings.initial_admin_full_name
        if not admin.timezone_name:
            admin.timezone_name = settings.initial_admin_timezone
        if admin.subscription_ends_at is None:
            admin.subscription_ends_at = datetime.now(timezone.utc) + timedelta(days=3650)
        session.commit()

    for method in session.scalars(select(PaymentMethod).where(PaymentMethod.owner_user_id.is_(None))).all():
        method.owner_user_id = admin.id
    for service in session.scalars(select(Service).where(Service.owner_user_id.is_(None))).all():
        service.owner_user_id = admin.id

    session.commit()
    session.refresh(admin)
    return admin


def seed_database(session: Session) -> None:
    if session.scalar(select(User.id)):
        return

    admin = User(
        username=settings.initial_admin_username,
        full_name=settings.initial_admin_full_name,
        email=settings.initial_admin_email,
        password_hash=hash_password(settings.initial_admin_password),
        avatar_url="https://images.unsplash.com/photo-1544723795-3fb6469f5b39?auto=format&fit=crop&w=300&q=80",
        timezone_name=settings.initial_admin_timezone,
        subscription_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
        is_admin=True,
    )
    session.add(admin)
    session.flush()

    payment_methods = [
        PaymentMethod(name="Bancamiga", owner_user_id=admin.id, notes="Banco principal para recargas", display_order=1, is_default=True),
        PaymentMethod(name="Binance", owner_user_id=admin.id, notes="Transferencias cripto", display_order=2),
        PaymentMethod(name="Pago Movil", owner_user_id=admin.id, notes="Pagos nacionales", display_order=3),
    ]
    session.add_all(payment_methods)

    services = [
        Service(name="Free Fire", owner_user_id=admin.id, notes="Diamantes y membresias", display_order=1, is_default=True),
        Service(name="Mobile Legends", owner_user_id=admin.id, notes="Recargas rapidas", display_order=2),
        Service(name="Call of Duty Mobile", owner_user_id=admin.id, notes="Packs en USD y Bs", display_order=3),
    ]
    session.add_all(services)

    session.add(AppSetting(exchange_rate_bs=Decimal("36.50"), history_retention_months=3, support_contact="Contacta a un admin"))
    session.flush()

    catalog = [
        (services[0], "100 Diamantes", Decimal("1.99"), 1),
        (services[0], "310 Diamantes", Decimal("4.99"), 2),
        (services[0], "1060 Diamantes", Decimal("14.99"), 3),
        (services[1], "86 Diamantes", Decimal("1.49"), 1),
        (services[1], "257 Diamantes", Decimal("3.99"), 2),
        (services[2], "80 CP", Decimal("0.99"), 1),
        (services[2], "420 CP", Decimal("4.49"), 2),
    ]
    packages = []
    for service, name, price, order in catalog:
        package = Package(service_id=service.id, name=name, usd_price=price, display_order=order)
        session.add(package)
        packages.append(package)

    session.add(Package(service_id=services[2].id, name="Pase semanal Bs", usd_price=Decimal("0.00"), bs_price=Decimal("145.00"), display_order=3))

    session.flush()

    reference = "321654987"
    digits = extract_digits(reference)
    expected_total_usd = Decimal("4.99")
    exchange_rate = Decimal("36.50")
    seed_sale = Sale(
        validation_month=current_month_bucket(admin.timezone_name),
        operator_timezone=admin.timezone_name,
        reference_raw=reference,
        reference_digits=digits,
        reference_last_6=build_suffix(digits, 6),
        reference_last_7=build_suffix(digits, 7),
        validation_key=build_suffix(digits, 6),
        validation_digits_used=6,
        amount_paid_value=expected_total_usd,
        amount_paid_currency="USD",
        amount_paid_usd=expected_total_usd,
        amount_paid_bs=expected_total_usd * exchange_rate,
        expected_total_usd=expected_total_usd,
        expected_total_bs=expected_total_usd * exchange_rate,
        payment_method_id=payment_methods[0].id,
        operator_id=admin.id,
        notes="Venta demo para probar alerta de duplicado.",
    )
    session.add(seed_sale)
    session.flush()

    session.add(
        SaleItem(
            sale_id=seed_sale.id,
            service_id=services[0].id,
            package_id=packages[1].id,
            service_name_snapshot=services[0].name,
            package_name_snapshot=packages[1].name,
            usd_price=packages[1].usd_price,
        )
    )

    session.commit()
