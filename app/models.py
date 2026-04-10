from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    exchange_rate_bs: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    pabilo_api_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pabilo_user_bank_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pabilo_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone_name: Mapped[str] = mapped_column(String(64), default="America/Caracas")
    subscription_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    sales: Mapped[list[Sale]] = relationship(back_populates="operator")
    extension_requests: Mapped[list[DaysExtensionRequest]] = relationship(back_populates="user")
    payment_method_access: Mapped[list[UserPaymentMethod]] = relationship(back_populates="user", cascade="all, delete-orphan")
    package_access: Mapped[list[UserPackage]] = relationship(back_populates="user", cascade="all, delete-orphan")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_payment_methods_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(3), default="BS")
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    sales: Mapped[list[Sale]] = relationship(back_populates="payment_method")
    services: Mapped[list[Service]] = relationship(back_populates="payment_method")
    user_access: Mapped[list[UserPaymentMethod]] = relationship(back_populates="payment_method", cascade="all, delete-orphan")


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_services_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    payment_method_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payment_methods.id"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    payment_method: Mapped[Optional[PaymentMethod]] = relationship(back_populates="services")
    packages: Mapped[list[Package]] = relationship(back_populates="service")


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    name: Mapped[str] = mapped_column(String(120))
    usd_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    bs_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    service: Mapped[Service] = relationship(back_populates="packages")
    sale_items: Mapped[list[SaleItem]] = relationship(back_populates="package")
    user_access: Mapped[list[UserPackage]] = relationship(back_populates="package", cascade="all, delete-orphan")


class UserPaymentMethod(Base):
    __tablename__ = "user_payment_methods"
    __table_args__ = (UniqueConstraint("user_id", "payment_method_id", name="uq_user_payment_method"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    payment_method_id: Mapped[int] = mapped_column(ForeignKey("payment_methods.id"))

    user: Mapped[User] = relationship(back_populates="payment_method_access")
    payment_method: Mapped[PaymentMethod] = relationship(back_populates="user_access")


class UserPackage(Base):
    __tablename__ = "user_packages"
    __table_args__ = (UniqueConstraint("user_id", "package_id", name="uq_user_package"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))

    user: Mapped[User] = relationship(back_populates="package_access")
    package: Mapped[Package] = relationship(back_populates="user_access")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    exchange_rate_bs: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("36.50"))
    history_retention_months: Mapped[int] = mapped_column(Integer, default=3)
    support_contact: Mapped[str] = mapped_column(String(255), default="Contacta a un administrador")


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("payment_method_id", "validation_month", "validation_key", name="uq_payment_month_validation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    validation_month: Mapped[date] = mapped_column(Date, index=True)
    operator_timezone: Mapped[str] = mapped_column(String(64))
    reference_raw: Mapped[str] = mapped_column(String(120))
    reference_digits: Mapped[str] = mapped_column(String(120), index=True)
    reference_last_6: Mapped[str] = mapped_column(String(6), index=True)
    reference_last_7: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    validation_key: Mapped[str] = mapped_column(String(12), index=True)
    validation_digits_used: Mapped[int] = mapped_column(Integer, default=6)
    amount_paid_value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount_paid_currency: Mapped[str] = mapped_column(String(3), default="BS")
    amount_paid_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount_paid_bs: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    expected_total_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    expected_total_bs: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_method_id: Mapped[int] = mapped_column(ForeignKey("payment_methods.id"))
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    payment_method: Mapped[PaymentMethod] = relationship(back_populates="sales")
    operator: Mapped[User] = relationship(back_populates="sales")
    items: Mapped[list[SaleItem]] = relationship(back_populates="sale", cascade="all, delete-orphan")


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    service_name_snapshot: Mapped[str] = mapped_column(String(80))
    package_name_snapshot: Mapped[str] = mapped_column(String(120))
    usd_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    sale: Mapped[Sale] = relationship(back_populates="items")
    package: Mapped[Package] = relationship(back_populates="sale_items")


class DaysExtensionRequest(Base):
    __tablename__ = "days_extension_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    requested_days: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(255), default="Necesito extender mi acceso")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="extension_requests")
