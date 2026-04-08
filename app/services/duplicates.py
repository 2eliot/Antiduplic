from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Sale


def extract_digits(reference: str) -> str:
    return "".join(character for character in reference if character.isdigit())


def build_suffix(value: str, preferred_length: int) -> str:
    usable_length = min(len(value), preferred_length)
    return value[-usable_length:] if usable_length else ""


def current_month_bucket(timezone_name: str, at: Optional[datetime] = None) -> date:
    local_now = (at or datetime.now(ZoneInfo(timezone_name))).astimezone(ZoneInfo(timezone_name))
    return date(local_now.year, local_now.month, 1)


def sale_to_duplicate_payload(sale: Sale) -> dict:
    local_time = sale.created_at.astimezone(ZoneInfo(sale.operator_timezone))
    return {
        "id": sale.id,
        "date": local_time.strftime("%d/%m/%Y"),
        "time": local_time.strftime("%I:%M %p"),
        "amount_paid_usd": f"{Decimal(sale.amount_paid_usd):.2f}",
        "amount_paid_bs": f"{Decimal(sale.amount_paid_bs):.2f}",
        "payment_method": sale.payment_method.name,
        "items": [
            {
                "service": item.service_name_snapshot,
                "package": item.package_name_snapshot,
            }
            for item in sale.items
        ],
    }


def check_duplicate_reference(session: Session, payment_method_id: int, reference: str, timezone_name: str) -> dict:
    digits = extract_digits(reference)
    if not digits:
        return {
            "duplicate": False,
            "digits": "",
            "last6": "",
            "last7": "",
            "warning": None,
        }

    month_bucket = current_month_bucket(timezone_name)
    suffix_6 = build_suffix(digits, 6)
    suffix_7 = build_suffix(digits, 7)

    duplicate_query = (
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method))
        .where(
            Sale.payment_method_id == payment_method_id,
            Sale.validation_month == month_bucket,
            Sale.validation_key == suffix_6,
        )
        .order_by(Sale.created_at.desc())
    )
    duplicate_sale = session.scalars(duplicate_query).first()

    has_last7_conflict = False
    if len(digits) >= 7 and suffix_7 != suffix_6:
        has_last7_conflict = session.scalar(
            select(Sale.id).where(
                Sale.payment_method_id == payment_method_id,
                Sale.validation_month == month_bucket,
                Sale.validation_key == suffix_7,
            )
        ) is not None

    can_validate_with_7 = bool(duplicate_sale and len(digits) >= 7 and suffix_7 != suffix_6 and not has_last7_conflict)

    return {
        "duplicate": duplicate_sale is not None,
        "digits": digits,
        "last6": suffix_6,
        "last7": suffix_7,
        "can_validate_with_7": can_validate_with_7,
        "warning": sale_to_duplicate_payload(duplicate_sale) if duplicate_sale else None,
    }
