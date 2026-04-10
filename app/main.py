from __future__ import annotations

import json
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, available_timezones

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import SessionLocal, engine, get_db
from app.models import AppSetting, Base, DaysExtensionRequest, Package, PaymentMethod, Sale, SaleItem, Service, User
from app.security import hash_password, verify_password
from app.seed import ensure_initial_admin, seed_database
from app.services.pabilo import verify_pabilo_reference
from app.services.duplicates import build_suffix, check_duplicate_reference, current_month_bucket, extract_digits


templates = Jinja2Templates(directory="templates")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
AVATAR_UPLOAD_DIR = STATIC_DIR / "uploads" / "avatars"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SUPPORTED_TIMEZONES = [
    "America/Caracas",
    "America/Bogota",
    "America/Panama",
    "America/Mexico_City",
    "America/Lima",
    "UTC",
]
SUPPORTED_CURRENCIES = {"USD", "BS"}


class SaleItemPayload(BaseModel):
    package_id: int


class CreateSalePayload(BaseModel):
    payment_method_id: int
    reference: str = Field(min_length=1)
    amount_paid_value: Optional[Decimal] = Field(default=None, gt=0)
    amount_paid_currency: Optional[str] = Field(default=None, pattern="^(USD|BS)$")
    force_seven_validation: bool = False
    notes: Optional[str] = None
    items: list[SaleItemPayload] = Field(min_length=1)


class PabiloReferencePayload(BaseModel):
    reference: str = Field(min_length=1)


def ensure_database_features() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return

    sqlite_mode = settings.database_url.startswith("sqlite")
    column_map = {
        "users": {
            "is_admin": "INTEGER NOT NULL DEFAULT 0" if sqlite_mode else "BOOLEAN NOT NULL DEFAULT FALSE",
            "exchange_rate_bs": "NUMERIC(10, 2)",
            "pabilo_api_key": "VARCHAR(255)",
            "pabilo_user_bank_id": "VARCHAR(100)",
            "pabilo_enabled": "INTEGER NOT NULL DEFAULT 0" if sqlite_mode else "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "payment_methods": {
            "owner_user_id": "INTEGER",
            "currency_code": "VARCHAR(3) NOT NULL DEFAULT 'BS'",
            "notes": "VARCHAR(255)",
        },
        "services": {
            "owner_user_id": "INTEGER",
            "payment_method_id": "INTEGER",
            "notes": "VARCHAR(255)",
        },
        "packages": {
            "bs_price": "NUMERIC(12, 2)",
        },
        "days_extension_requests": {
            "requested_days": "INTEGER NOT NULL DEFAULT 0",
            "reviewed_at": "TIMESTAMP",
            "reviewed_by_id": "INTEGER",
        },
    }

    sqlite_catalog_tables = {
        "payment_methods": {
            "constraint_name": "uq_payment_methods_owner_name",
            "columns": ["id", "name", "owner_user_id", "currency_code", "notes", "display_order", "is_default", "is_active"],
            "ddl": """
                CREATE TABLE payment_methods__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(80) NOT NULL,
                    owner_user_id INTEGER,
                    currency_code VARCHAR(3) NOT NULL DEFAULT 'BS',
                    notes VARCHAR(255),
                    display_order INTEGER NOT NULL,
                    is_default BOOLEAN NOT NULL,
                    is_active BOOLEAN NOT NULL,
                    CONSTRAINT uq_payment_methods_owner_name UNIQUE (owner_user_id, name),
                    FOREIGN KEY(owner_user_id) REFERENCES users (id)
                )
            """,
        },
        "services": {
            "constraint_name": "uq_services_owner_name",
            "columns": ["id", "name", "owner_user_id", "payment_method_id", "notes", "display_order", "is_default", "is_active"],
            "ddl": """
                CREATE TABLE services__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(80) NOT NULL,
                    owner_user_id INTEGER,
                    payment_method_id INTEGER,
                    notes VARCHAR(255),
                    display_order INTEGER NOT NULL,
                    is_default BOOLEAN NOT NULL,
                    is_active BOOLEAN NOT NULL,
                    CONSTRAINT uq_services_owner_name UNIQUE (owner_user_id, name),
                    FOREIGN KEY(owner_user_id) REFERENCES users (id),
                    FOREIGN KEY(payment_method_id) REFERENCES payment_methods (id)
                )
            """,
        },
    }

    def has_global_name_unique(table_name: str) -> bool:
        table_inspector = inspect(engine)
        return any((constraint.get("column_names") or []) == ["name"] for constraint in table_inspector.get_unique_constraints(table_name))

    def has_owner_name_unique(table_name: str, constraint_name: str) -> bool:
        table_inspector = inspect(engine)
        constraints = table_inspector.get_unique_constraints(table_name)
        for constraint in constraints:
            columns = constraint.get("column_names") or []
            if constraint.get("name") == constraint_name:
                return True
            if set(columns) == {"owner_user_id", "name"}:
                return True
        return False

    def rebuild_sqlite_catalog_table(connection, table_name: str) -> None:
        table_config = sqlite_catalog_tables[table_name]
        column_csv = ", ".join(table_config["columns"])
        connection.exec_driver_sql(table_config["ddl"])
        connection.exec_driver_sql(
            f"INSERT INTO {table_name}__new ({column_csv}) SELECT {column_csv} FROM {table_name}"
        )
        connection.exec_driver_sql(f"DROP TABLE {table_name}")
        connection.exec_driver_sql(f"ALTER TABLE {table_name}__new RENAME TO {table_name}")

    def migrate_catalog_uniqueness(connection) -> None:
        if sqlite_mode:
            needs_sqlite_migration = any(
                has_global_name_unique(table_name) or not has_owner_name_unique(table_name, table_config["constraint_name"])
                for table_name, table_config in sqlite_catalog_tables.items()
            )
            if not needs_sqlite_migration:
                return

            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                for table_name in sqlite_catalog_tables:
                    if has_global_name_unique(table_name) or not has_owner_name_unique(table_name, sqlite_catalog_tables[table_name]["constraint_name"]):
                        rebuild_sqlite_catalog_table(connection, table_name)
                connection.commit()
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            return

        for table_name, table_config in sqlite_catalog_tables.items():
            table_inspector = inspect(engine)
            unique_constraints = table_inspector.get_unique_constraints(table_name)
            global_constraints = [
                constraint for constraint in unique_constraints if (constraint.get("column_names") or []) == ["name"] and constraint.get("name")
            ]
            for constraint in global_constraints:
                connection.exec_driver_sql(f"ALTER TABLE {table_name} DROP CONSTRAINT {constraint['name']}")
            if not has_owner_name_unique(table_name, table_config["constraint_name"]):
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD CONSTRAINT {table_config['constraint_name']} UNIQUE (owner_user_id, name)"
                )

    with engine.connect() as connection:
        for table_name, columns in column_map.items():
            if not inspector.has_table(table_name):
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name not in existing_columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")
        connection.commit()
        migrate_catalog_uniqueness(connection)
        connection.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_database_features()
    AVATAR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        if settings.seed_demo_data:
            seed_database(session)
        else:
            ensure_initial_admin(session)
        normalize_service_payment_links(session)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.session_https_only)
app.mount("/static", StaticFiles(directory="static"), name="static")


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_setting(db: Session) -> AppSetting:
    setting = db.scalar(select(AppSetting).where(AppSetting.id == 1))
    if not setting:
        setting = AppSetting(id=1, exchange_rate_bs=Decimal("36.50"), history_retention_months=3)
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def get_effective_exchange_rate(current_user: User, setting: AppSetting) -> Decimal:
    if current_user.exchange_rate_bs is not None:
        return money(Decimal(current_user.exchange_rate_bs))
    return money(Decimal(setting.exchange_rate_bs))


def normalize_currency_code(raw_value: str) -> str:
    normalized_value = (raw_value or "").strip().upper()
    if normalized_value not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La moneda debe ser USD o BS.")
    return normalized_value


def normalize_service_payment_links(db: Session) -> None:
    services_without_method = db.scalars(select(Service).where(Service.payment_method_id.is_(None))).all()
    changed = False
    for service in services_without_method:
        statement = select(PaymentMethod).where(PaymentMethod.is_active.is_(True))
        if service.owner_user_id is None:
            statement = statement.where(PaymentMethod.owner_user_id.is_(None))
        else:
            statement = statement.where(PaymentMethod.owner_user_id == service.owner_user_id)
        method = db.scalar(statement.order_by(PaymentMethod.is_default.desc(), PaymentMethod.display_order, PaymentMethod.id))
        if method:
            service.payment_method_id = method.id
            changed = True
    if changed:
        db.commit()


def package_price_breakdown(package: Package, exchange_rate: Decimal) -> dict:
    if package.bs_price is not None:
        bs_value = money(Decimal(package.bs_price))
        usd_value = money(bs_value / exchange_rate) if exchange_rate else Decimal("0.00")
        display_value = f"Bs {bs_value:.2f}"
        display_currency = "BS"
    else:
        usd_value = money(Decimal(package.usd_price))
        bs_value = money(usd_value * exchange_rate)
        display_value = f"USD {usd_value:.2f}"
        display_currency = "USD"
    return {
        "usd": usd_value,
        "bs": bs_value,
        "display_value": display_value,
        "display_currency": display_currency,
    }


def recalculate_sale_exchange_totals(sale: Sale, exchange_rate: Decimal) -> None:
    sale.amount_paid_bs = money(Decimal(sale.amount_paid_usd) * exchange_rate)
    sale.expected_total_bs = money(Decimal(sale.expected_total_usd) * exchange_rate)
    if sale.amount_paid_currency == "BS":
        sale.amount_paid_value = sale.amount_paid_bs


def apply_extension_days(user: User, days: int) -> None:
    baseline = user.subscription_ends_at
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=timezone.utc)
    baseline = max(baseline, datetime.now(timezone.utc))
    user.subscription_ends_at = baseline + timedelta(days=days)


def local_day_bounds(day_value: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start_local = datetime.combine(day_value, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def last_n_local_day_bounds(timezone_name: str, days: int) -> tuple[date, date, datetime, datetime]:
    today_local = localize(datetime.now(timezone.utc), timezone_name).date()
    start_date = today_local - timedelta(days=max(days - 1, 0))
    start_utc, _ = local_day_bounds(start_date, timezone_name)
    _, end_utc = local_day_bounds(today_local, timezone_name)
    return start_date, today_local, start_utc, end_utc


def parse_iso_date(raw_value: Optional[str]) -> Optional[date]:
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, "%Y-%m-%d").date()
    except ValueError:
        return None


def set_payment_method_default(db: Session, selected_id: int, owner_user_id: Optional[int]) -> None:
    statement = select(PaymentMethod)
    if owner_user_id is None:
        statement = statement.where(PaymentMethod.owner_user_id.is_(None))
    else:
        statement = statement.where(PaymentMethod.owner_user_id == owner_user_id)
    methods = db.scalars(statement).all()
    for method in methods:
        method.is_default = method.id == selected_id


def set_service_default(db: Session, selected_id: int, owner_user_id: Optional[int]) -> None:
    statement = select(Service)
    if owner_user_id is None:
        statement = statement.where(Service.owner_user_id.is_(None))
    else:
        statement = statement.where(Service.owner_user_id == owner_user_id)
    services = db.scalars(statement).all()
    for service in services:
        service.is_default = service.id == selected_id


def apply_catalog_owner_filter(statement, owner_column, user: User):
    if user.is_admin:
        return statement.where(or_(owner_column == user.id, owner_column.is_(None)))
    return statement.where(owner_column == user.id)


def catalog_name_exists(db: Session, model, user: User, name: str, *, exclude_id: Optional[int] = None) -> bool:
    normalized_name = name.strip().lower()
    statement = select(model.id).where(func.lower(model.name) == normalized_name)
    statement = apply_catalog_owner_filter(statement, model.owner_user_id, user)
    if exclude_id is not None:
        statement = statement.where(model.id != exclude_id)
    return db.scalar(statement) is not None


def consume_flash_message(request: Request, key: str) -> Optional[str]:
    return request.session.pop(key, None)


def build_profile_context(request: Request, db: Session, current_user: User, *, profile_error: Optional[str] = None) -> dict:
    setting = get_setting(db)
    timezone_options = [timezone_name for timezone_name in SUPPORTED_TIMEZONES if timezone_name in available_timezones()]
    if current_user.timezone_name not in timezone_options:
        timezone_options.insert(0, current_user.timezone_name)

    return {
        "timezone_options": timezone_options,
        "days_left": calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name),
        "support_contact": setting.support_contact,
        "latest_request": db.scalar(
            select(DaysExtensionRequest)
            .where(DaysExtensionRequest.user_id == current_user.id)
            .order_by(DaysExtensionRequest.created_at.desc())
        ),
        "profile_error": profile_error,
        "pabilo_ready": bool(current_user.pabilo_api_key and current_user.pabilo_user_bank_id) if current_user.is_admin else False,
        **layout_context(request, current_user),
    }


def resolve_local_static_path(public_url: Optional[str]) -> Optional[Path]:
    if not public_url or not public_url.startswith("/static/"):
        return None
    relative_path = public_url.removeprefix("/static/")
    local_path = STATIC_DIR / Path(relative_path)
    try:
        local_path.resolve().relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    return local_path


def save_avatar_upload(avatar_file: UploadFile, user_id: int, existing_avatar_url: Optional[str]) -> str:
    filename = (avatar_file.filename or "").strip()
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_IMAGE_SUFFIXES or not (avatar_file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La imagen debe ser JPG, PNG, GIF o WEBP.")

    AVATAR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    relative_path = Path("uploads") / "avatars" / f"user-{user_id}-{uuid4().hex}{suffix}"
    target_path = STATIC_DIR / relative_path
    avatar_file.file.seek(0)
    with target_path.open("wb") as output_file:
        shutil.copyfileobj(avatar_file.file, output_file)

    previous_file = resolve_local_static_path(existing_avatar_url)
    if previous_file and previous_file.exists() and previous_file != target_path:
        previous_file.unlink(missing_ok=True)

    return f"/static/{relative_path.as_posix()}"


def user_can_manage_catalog(user: User) -> bool:
    return user.is_admin or calculate_days_left(user.subscription_ends_at, user.timezone_name) > 0


def get_managed_payment_methods(db: Session, user: User) -> list[PaymentMethod]:
    statement = apply_catalog_owner_filter(select(PaymentMethod), PaymentMethod.owner_user_id, user)
    statement = statement.order_by(PaymentMethod.display_order, PaymentMethod.name)
    return db.scalars(statement).all()


def get_managed_services(db: Session, user: User) -> list[Service]:
    statement = apply_catalog_owner_filter(select(Service).options(joinedload(Service.packages), joinedload(Service.payment_method)), Service.owner_user_id, user)
    statement = statement.order_by(Service.display_order, Service.name)
    return db.scalars(statement).unique().all()


def get_managed_payment_method(db: Session, user: User, method_id: int) -> PaymentMethod:
    statement = select(PaymentMethod).where(PaymentMethod.id == method_id)
    statement = apply_catalog_owner_filter(statement, PaymentMethod.owner_user_id, user)
    method = db.scalar(statement)
    if not method:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metodo no encontrado.")
    return method


def get_managed_service(db: Session, user: User, service_id: int) -> Service:
    statement = select(Service).options(joinedload(Service.payment_method)).where(Service.id == service_id)
    statement = apply_catalog_owner_filter(statement, Service.owner_user_id, user)
    service = db.scalar(statement)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")
    return service


def get_managed_payment_method_for_service(db: Session, user: User, payment_method_id: int) -> PaymentMethod:
    method = get_managed_payment_method(db, user, payment_method_id)
    if not method.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El método de pago seleccionado está inactivo.")
    return method


def get_managed_package(db: Session, user: User, package_id: int) -> Package:
    statement = select(Package).options(joinedload(Package.service)).where(Package.id == package_id)
    statement = statement.join(Package.service)
    statement = apply_catalog_owner_filter(statement, Service.owner_user_id, user)
    package = db.scalar(statement)
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paquete no encontrado.")
    return package


def get_accessible_payment_methods(db: Session, user: User) -> list[PaymentMethod]:
    statement = select(PaymentMethod).where(PaymentMethod.is_active.is_(True))
    statement = apply_catalog_owner_filter(statement, PaymentMethod.owner_user_id, user)
    statement = statement.order_by(PaymentMethod.display_order, PaymentMethod.name)
    return db.scalars(statement).all()


def get_accessible_services_and_packages(db: Session, user: User) -> tuple[list[Service], set[int]]:
    package_statement = (
        select(Package.id)
        .join(Package.service)
        .where(Service.is_active.is_(True), Package.is_active.is_(True))
    )
    package_statement = apply_catalog_owner_filter(package_statement, Service.owner_user_id, user)
    package_ids = set(
        db.scalars(package_statement).all()
    )
    if not package_ids:
        return [], set()

    service_statement = (
        select(Service)
        .options(joinedload(Service.packages), joinedload(Service.payment_method))
        .join(Service.packages)
        .where(Service.is_active.is_(True), Package.id.in_(package_ids))
    )
    service_statement = apply_catalog_owner_filter(service_statement, Service.owner_user_id, user)
    services = db.scalars(service_statement.order_by(Service.display_order)).unique().all()
    return services, package_ids


def localize(dt: datetime, timezone_name: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(timezone_name))


def calculate_days_left(subscription_ends_at: datetime, timezone_name: str) -> int:
    local_end = localize(subscription_ends_at, timezone_name)
    local_now = datetime.now(ZoneInfo(timezone_name))
    delta = local_end.date() - local_now.date()
    return max(delta.days, 0)


def user_can_operate(user: User, days_left: int, payment_methods: list[PaymentMethod], allowed_package_ids: set[int]) -> bool:
    if user.is_admin:
        return bool(payment_methods) and bool(allowed_package_ids)
    return days_left > 0 and bool(payment_methods) and bool(allowed_package_ids)


def user_can_verify_pabilo(current_user: User) -> bool:
    return current_user.is_admin or current_user.pabilo_enabled


def get_pabilo_credentials_owner(db: Session, current_user: User) -> Optional[User]:
    if current_user.is_admin and (current_user.pabilo_api_key or current_user.pabilo_user_bank_id):
        return current_user
    return db.scalar(
        select(User)
        .where(
            User.is_admin.is_(True),
            User.is_active.is_(True),
            User.pabilo_api_key.is_not(None),
            User.pabilo_user_bank_id.is_not(None),
        )
        .order_by(User.id.asc())
    )


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    user = db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if not user:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def get_admin_user(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Solo un administrador puede entrar aqui.")
    return current_user


def get_catalog_manager_user(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user_can_manage_catalog(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Necesitas días activos para gestionar tu catálogo.")
    return current_user


def sidebar_context(user: User) -> dict:
    return {
        "user_card": {
            "name": user.full_name,
            "avatar_url": user.avatar_url,
            "days_left": calculate_days_left(user.subscription_ends_at, user.timezone_name),
            "username": user.username,
        }
    }


def asset_url(filename: str) -> str:
    asset_path = STATIC_DIR / filename
    version = int(asset_path.stat().st_mtime_ns) if asset_path.exists() else 0
    return f"/static/{filename}?v={version}"


templates.env.globals["asset_url"] = asset_url


def layout_context(request: Request, current_user: User) -> dict:
    return {
        "request": request,
        "app_name": settings.app_name,
        "current_user": current_user,
        "can_manage_catalog": user_can_manage_catalog(current_user),
        **sidebar_context(current_user),
    }


def serialize_catalog(services: list[Service], exchange_rate: Decimal, allowed_package_ids: Optional[set[int]] = None) -> list[dict]:
    return [
        {
            "id": service.id,
            "name": service.name,
            "is_default": service.is_default,
            "payment_method_id": service.payment_method_id,
            "packages": [
                {
                    "id": package.id,
                    "name": package.name,
                    "usd_price": f"{package_price_breakdown(package, exchange_rate)['usd']:.2f}",
                    "bs_price": f"{package_price_breakdown(package, exchange_rate)['bs']:.2f}",
                    "display_price": package_price_breakdown(package, exchange_rate)["display_value"],
                    "display_currency": package_price_breakdown(package, exchange_rate)["display_currency"],
                    "service_id": service.id,
                }
                for package in sorted(service.packages, key=lambda item: item.display_order)
                if package.is_active and (allowed_package_ids is None or package.id in allowed_package_ids)
            ],
        }
        for service in services
        if service.is_active and any(package.is_active and (allowed_package_ids is None or package.id in allowed_package_ids) for package in service.packages)
    ]


def sale_card_payload(sale: Sale) -> dict:
    local_time = localize(sale.created_at, sale.operator_timezone)
    return {
        "id": sale.id,
        "created_at": local_time.strftime("%d/%m/%Y %I:%M %p"),
        "created_day": local_time.strftime("%Y-%m-%d"),
        "payment_method": sale.payment_method.name,
        "payment_method_currency": sale.payment_method.currency_code,
        "operator_username": sale.operator.username,
        "operator_email": sale.operator.email,
        "reference_raw": sale.reference_raw,
        "reference": sale.reference_digits,
        "reference_short": sale.reference_last_6,
        "validation_digits_used": sale.validation_digits_used,
        "amount_paid_usd": f"{Decimal(sale.amount_paid_usd):.2f}",
        "amount_paid_bs": f"{Decimal(sale.amount_paid_bs):.2f}",
        "expected_total_usd": f"{Decimal(sale.expected_total_usd):.2f}",
        "expected_total_bs": f"{Decimal(sale.expected_total_bs):.2f}",
        "primary_service": sale.items[0].service_name_snapshot if sale.items else "-",
        "notes": sale.notes,
        "items": [
            {
                "service": item.service_name_snapshot,
                "package": item.package_name_snapshot,
                "usd_price": f"{Decimal(item.usd_price):.2f}",
            }
            for item in sale.items
        ],
    }


def build_recent_sales_summary(sales: list[Sale], timezone_name: str, *, days: int = 7) -> dict:
    start_date, end_date, _, _ = last_n_local_day_bounds(timezone_name, days)
    buckets: dict[date, dict] = {}
    cursor = start_date
    while cursor <= end_date:
        buckets[cursor] = {
            "label": cursor.strftime("%d/%m"),
            "sales_count": 0,
            "total_usd": Decimal("0.00"),
            "total_bs": Decimal("0.00"),
        }
        cursor += timedelta(days=1)

    for sale in sales:
        sale_day = localize(sale.created_at, timezone_name).date()
        if sale_day not in buckets:
            continue
        buckets[sale_day]["sales_count"] += 1
        buckets[sale_day]["total_usd"] += Decimal(sale.amount_paid_usd)
        buckets[sale_day]["total_bs"] += Decimal(sale.amount_paid_bs)

    total_sales = sum(bucket["sales_count"] for bucket in buckets.values())
    total_usd = sum((bucket["total_usd"] for bucket in buckets.values()), Decimal("0.00"))
    total_bs = sum((bucket["total_bs"] for bucket in buckets.values()), Decimal("0.00"))
    avg_ticket = (total_usd / total_sales) if total_sales else Decimal("0.00")
    best_day_bucket = max(
        buckets.values(),
        key=lambda bucket: (bucket["total_usd"], bucket["sales_count"]),
        default={"label": "--/--", "total_usd": Decimal("0.00"), "sales_count": 0},
    )

    return {
        "range_label": f"{start_date.strftime('%d/%m')} - {end_date.strftime('%d/%m')}",
        "sales_count": total_sales,
        "total_usd": f"{total_usd:.2f}",
        "total_bs": f"{total_bs:.2f}",
        "avg_ticket_usd": f"{avg_ticket:.2f}",
        "best_day": {
            "label": best_day_bucket["label"],
            "total_usd": f"{best_day_bucket['total_usd']:.2f}",
            "sales_count": best_day_bucket["sales_count"],
        },
        "days": [
            {
                "label": bucket["label"],
                "sales_count": bucket["sales_count"],
                "total_usd": f"{bucket['total_usd']:.2f}",
                "total_bs": f"{bucket['total_bs']:.2f}",
            }
            for bucket in buckets.values()
        ],
    }


def build_history_dashboard(sales: list[Sale], timezone_name: str, *, start_date: Optional[date], end_date: Optional[date]) -> dict:
    total_usd = sum((Decimal(sale.amount_paid_usd) for sale in sales), Decimal("0.00"))
    total_bs = sum((Decimal(sale.amount_paid_bs) for sale in sales), Decimal("0.00"))
    avg_ticket = (total_usd / len(sales)) if sales else Decimal("0.00")

    method_counter = Counter(sale.payment_method.name for sale in sales)
    service_counter = Counter(item.service_name_snapshot for sale in sales for item in sale.items)
    package_counter = Counter(item.package_name_snapshot for sale in sales for item in sale.items)

    top_method = method_counter.most_common(1)[0] if method_counter else ("Sin datos", 0)
    top_service = service_counter.most_common(1)[0] if service_counter else ("Sin datos", 0)
    top_package = package_counter.most_common(1)[0] if package_counter else ("Sin datos", 0)

    series_map: dict[str, Decimal] = {}
    for sale in sales:
        local_day = localize(sale.created_at, timezone_name).date().isoformat()
        series_map.setdefault(local_day, Decimal("0.00"))
        series_map[local_day] += Decimal(sale.amount_paid_usd)

    if start_date and end_date and start_date <= end_date:
        cursor = start_date
        chart_labels = []
        chart_values = []
        while cursor <= end_date:
            key = cursor.isoformat()
            chart_labels.append(key)
            chart_values.append(float(series_map.get(key, Decimal("0.00"))))
            cursor += timedelta(days=1)
    else:
        chart_labels = sorted(series_map.keys())
        chart_values = [float(series_map[label]) for label in chart_labels]

    return {
        "kpis": {
            "sales_count": len(sales),
            "total_usd": f"{total_usd:.2f}",
            "total_bs": f"{total_bs:.2f}",
            "avg_ticket_usd": f"{avg_ticket:.2f}",
        },
        "top_method": {"name": top_method[0], "count": top_method[1]},
        "top_service": {"name": top_service[0], "count": top_service[1]},
        "top_package": {"name": top_package[0], "count": top_package[1]},
        "chart_labels": chart_labels,
        "chart_values": chart_values,
    }


def render_login_template(
    request: Request,
    *,
    login_error: Optional[str] = None,
    login_email: str = "",
    register_error: Optional[str] = None,
    register_values: Optional[dict] = None,
):
    values = register_values or {}
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "error": login_error,
            "login_email": login_email,
            "register_error": register_error,
            "register_values": {
                "full_name": values.get("full_name", ""),
                "username": values.get("username", ""),
                "email": values.get("email", ""),
                "timezone_name": values.get("timezone_name", "America/Caracas"),
            },
            "timezone_options": SUPPORTED_TIMEZONES,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    location = exc.headers.get("Location") if exc.headers else None
    if exc.status_code == status.HTTP_303_SEE_OTHER and location:
        return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return render_login_template(request)


@app.post("/login")
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Optional[str] = Form(None),
    username: Optional[str] = Form(None),
    password: str = Form(...),
):
    normalized_email = (email or username or "").strip().lower()
    if not normalized_email:
        return render_login_template(request, login_error="Debes indicar tu correo.")
    user = db.scalar(select(User).where(func.lower(User.email) == normalized_email, User.is_active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        return render_login_template(request, login_error="Credenciales inválidas.", login_email=normalized_email)

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/register")
def register(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    full_name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    timezone_name: str = Form("America/Caracas"),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    form_values = {
        "full_name": full_name.strip(),
        "username": username.strip(),
        "email": email.strip().lower(),
        "timezone_name": timezone_name,
    }

    if len(password) < 6:
        return render_login_template(
            request,
            register_error="La contraseña debe tener al menos 6 caracteres.",
            register_values=form_values,
        )
    if password != confirm_password:
        return render_login_template(
            request,
            register_error="La confirmación de contraseña no coincide.",
            register_values=form_values,
        )
    if timezone_name not in SUPPORTED_TIMEZONES:
        timezone_name = "America/Caracas"
        form_values["timezone_name"] = timezone_name

    existing_user = db.scalar(select(User).where(User.username == form_values["username"]))
    if existing_user:
        return render_login_template(
            request,
            register_error="Ese nombre de usuario ya existe.",
            register_values=form_values,
        )

    existing_email = db.scalar(select(User).where(User.email == form_values["email"]))
    if existing_email:
        return render_login_template(
            request,
            register_error="Ese correo ya está registrado.",
            register_values=form_values,
        )

    user = User(
        username=form_values["username"],
        full_name=form_values["full_name"],
        email=form_values["email"],
        password_hash=hash_password(password),
        timezone_name=timezone_name,
        subscription_ends_at=datetime.now(timezone.utc),
        is_active=True,
        is_admin=False,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render_login_template(
            request,
            register_error="No se pudo crear la cuenta. Verifica usuario y correo.",
            register_values=form_values,
        )

    db.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/dashboard")
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    payment_methods = get_accessible_payment_methods(db, current_user)
    services, allowed_package_ids = get_accessible_services_and_packages(db, current_user)
    setting = get_setting(db)
    exchange_rate = get_effective_exchange_rate(current_user, setting)
    days_left = calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name)
    can_operate = user_can_operate(current_user, days_left, payment_methods, allowed_package_ids)
    recent_sales = db.scalars(
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method), joinedload(Sale.operator))
        .where(Sale.operator_id == current_user.id)
        .order_by(Sale.created_at.desc())
        .limit(5)
    ).unique().all()
    exchange_rate_feedback = consume_flash_message(request, "dashboard_exchange_rate_feedback")

    context = {
        "payment_methods": payment_methods,
        "services": services,
        "exchange_rate": f"{exchange_rate:.2f}",
        "exchange_rate_feedback": exchange_rate_feedback,
        "can_verify_pabilo": user_can_verify_pabilo(current_user),
        "catalog_json": json.dumps(serialize_catalog(services, exchange_rate, allowed_package_ids if not current_user.is_admin else None)),
        "timezone_name": current_user.timezone_name,
        "days_left": days_left,
        "has_access": bool(payment_methods and allowed_package_ids),
        "can_operate": can_operate,
        "recent_sales": [sale_card_payload(sale) for sale in recent_sales],
        **layout_context(request, current_user),
    }
    return templates.TemplateResponse("dashboard.html", context)


@app.post("/dashboard/exchange-rate")
def update_dashboard_exchange_rate(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    exchange_rate_bs: Decimal = Form(...),
):
    next_rate = money(exchange_rate_bs)
    if next_rate <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La tasa debe ser mayor a cero.")

    current_user.exchange_rate_bs = next_rate

    sales = db.scalars(select(Sale).where(Sale.operator_id == current_user.id)).all()
    for sale in sales:
        recalculate_sale_exchange_totals(sale, next_rate)

    db.commit()
    request.session["dashboard_exchange_rate_feedback"] = (
        f"Tu tasa fue actualizada a Bs {next_rate:.2f}. Todo tu historial fue recalculado con ese valor."
    )
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/payment-methods")
def payment_methods_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    methods = get_managed_payment_methods(db, current_user)
    error_message = consume_flash_message(request, "payment_methods_error")
    return templates.TemplateResponse(
        "payment_methods.html",
        {
            "payment_methods": methods,
            "page_error": error_message,
            **layout_context(request, current_user),
        },
    )


@app.post("/payment-methods")
def create_payment_method(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    currency_code: str = Form("BS"),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    owner_user_id = current_user.id
    normalized_name = name.strip()
    normalized_currency = normalize_currency_code(currency_code)
    if catalog_name_exists(db, PaymentMethod, current_user, normalized_name):
        request.session["payment_methods_error"] = "Ya existe un método de pago con ese nombre."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    method = PaymentMethod(
        name=normalized_name,
        owner_user_id=owner_user_id,
        currency_code=normalized_currency,
        notes=notes.strip() or None,
        display_order=display_order,
    )
    db.add(method)
    try:
        db.flush()
        if is_default:
            set_payment_method_default(db, method.id, owner_user_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        request.session["payment_methods_error"] = "Ya existe un método de pago con ese nombre."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/payment-methods/{method_id}/update")
def update_payment_method(
    method_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    currency_code: str = Form("BS"),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    method = get_managed_payment_method(db, current_user, method_id)
    normalized_name = name.strip()
    normalized_currency = normalize_currency_code(currency_code)
    if catalog_name_exists(db, PaymentMethod, current_user, normalized_name, exclude_id=method.id):
        request.session["payment_methods_error"] = "Ya existe un método de pago con ese nombre."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    method.name = normalized_name
    method.currency_code = normalized_currency
    method.notes = notes.strip() or None
    method.display_order = display_order
    if is_default:
        set_payment_method_default(db, method.id, method.owner_user_id)
    elif method.is_default:
        method.is_default = False
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        request.session["payment_methods_error"] = "Ya existe un método de pago con ese nombre."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/payment-methods/{method_id}/toggle")
def toggle_payment_method(
    method_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    method = get_managed_payment_method(db, current_user, method_id)
    method.is_active = not method.is_active
    if not method.is_active and method.is_default:
        method.is_default = False
    db.commit()
    return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/payment-methods/{method_id}/delete")
def delete_payment_method(
    method_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    method = get_managed_payment_method(db, current_user, method_id)
    linked_services = db.scalar(select(func.count()).select_from(Service).where(Service.payment_method_id == method.id)) or 0
    linked_sales = db.scalar(select(func.count()).select_from(Sale).where(Sale.payment_method_id == method.id)) or 0
    if linked_services:
        request.session["payment_methods_error"] = "No puedes borrar este método porque tiene servicios asignados."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    if linked_sales:
        request.session["payment_methods_error"] = "No puedes borrar este método porque ya tiene ventas registradas."
        return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)
    db.delete(method)
    db.commit()
    return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/services")
def services_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    setting = get_setting(db)
    exchange_rate = get_effective_exchange_rate(current_user, setting)
    services = get_managed_services(db, current_user)
    error_message = consume_flash_message(request, "services_error")
    return templates.TemplateResponse(
        "services.html",
        {
            "services": services,
            "payment_methods": get_managed_payment_methods(db, current_user),
            "exchange_rate": exchange_rate,
            "page_error": error_message,
            **layout_context(request, current_user),
        },
    )


@app.post("/services")
def create_service(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    payment_method_id: int = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    owner_user_id = current_user.id
    normalized_name = name.strip()
    payment_method = get_managed_payment_method_for_service(db, current_user, payment_method_id)
    if catalog_name_exists(db, Service, current_user, normalized_name):
        request.session["services_error"] = "Ya existe un servicio con ese nombre."
        return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)
    service = Service(
        name=normalized_name,
        owner_user_id=owner_user_id,
        payment_method_id=payment_method.id,
        notes=notes.strip() or None,
        display_order=display_order,
    )
    db.add(service)
    try:
        db.flush()
        if is_default:
            set_service_default(db, service.id, owner_user_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        request.session["services_error"] = "Ya existe un servicio con ese nombre."
        return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/services/{service_id}/update")
def update_service(
    service_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    payment_method_id: int = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    service = get_managed_service(db, current_user, service_id)
    normalized_name = name.strip()
    payment_method = get_managed_payment_method_for_service(db, current_user, payment_method_id)
    if catalog_name_exists(db, Service, current_user, normalized_name, exclude_id=service.id):
        request.session["services_error"] = "Ya existe un servicio con ese nombre."
        return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)
    service.name = normalized_name
    service.payment_method_id = payment_method.id
    service.notes = notes.strip() or None
    service.display_order = display_order
    if is_default:
        set_service_default(db, service.id, service.owner_user_id)
    elif service.is_default:
        service.is_default = False
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        request.session["services_error"] = "Ya existe un servicio con ese nombre."
        return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/services/{service_id}/toggle")
def toggle_service(
    service_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    service = get_managed_service(db, current_user, service_id)
    service.is_active = not service.is_active
    if not service.is_active and service.is_default:
        service.is_default = False
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/services/{service_id}/delete")
def delete_service(
    service_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    service = get_managed_service(db, current_user, service_id)
    package_ids = db.scalars(select(Package.id).where(Package.service_id == service.id)).all()
    linked_sale_items = 0
    if package_ids:
        linked_sale_items = db.scalar(select(func.count()).select_from(SaleItem).where(SaleItem.package_id.in_(package_ids))) or 0
    if linked_sale_items:
        request.session["services_error"] = "No puedes borrar este servicio porque alguno de sus paquetes ya tiene ventas registradas."
        return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)
    packages = db.scalars(select(Package).where(Package.service_id == service.id)).all()
    for package in packages:
        db.delete(package)
    db.delete(service)
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/services/{service_id}/packages")
def create_package(
    service_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    price_currency: str = Form(...),
    price_value: Decimal = Form(...),
    display_order: int = Form(0),
):
    get_managed_service(db, current_user, service_id)
    package = Package(service_id=service_id, name=name.strip(), usd_price=Decimal("0.00"), display_order=display_order)
    if price_currency == "BS":
        package.bs_price = money(price_value)
    else:
        package.usd_price = money(price_value)
        package.bs_price = None
    db.add(package)
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/packages/{package_id}/update")
def update_package(
    package_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    price_currency: str = Form(...),
    price_value: Decimal = Form(...),
    display_order: int = Form(0),
):
    package = get_managed_package(db, current_user, package_id)
    package.name = name.strip()
    package.display_order = display_order
    if price_currency == "BS":
        package.usd_price = Decimal("0.00")
        package.bs_price = money(price_value)
    else:
        package.usd_price = money(price_value)
        package.bs_price = None
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/packages/{package_id}/toggle")
def toggle_package(
    package_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    package = get_managed_package(db, current_user, package_id)
    package.is_active = not package.is_active
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/history")
def history_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    service_id: Optional[str] = None,
    preset: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sale_day: Optional[str] = None,
):
    setting = get_setting(db)
    since = datetime.now(timezone.utc) - timedelta(days=setting.history_retention_months * 31)
    statement = (
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method), joinedload(Sale.operator))
        .join(Sale.operator)
        .where(Sale.created_at >= since, Sale.operator_id == current_user.id)
        .order_by(Sale.created_at.desc())
    )

    if q:
        query = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Sale.reference_digits.ilike(query),
                User.email.ilike(query),
                User.username.ilike(query),
            )
        )
    payment_method_filter = int(payment_method_id) if payment_method_id else None
    service_filter = int(service_id) if service_id else None
    if payment_method_filter:
        statement = statement.where(Sale.payment_method_id == payment_method_filter)
    if service_filter:
        statement = statement.join(Sale.items).where(SaleItem.service_id == service_filter)
    today_local = localize(datetime.now(timezone.utc), current_user.timezone_name).date()
    start_date = parse_iso_date(date_from)
    end_date = parse_iso_date(date_to)
    selected_day = parse_iso_date(sale_day)

    if selected_day and not start_date and not end_date:
        start_date = selected_day
        end_date = selected_day

    active_preset = (preset or "").strip().lower()
    if active_preset == "today":
        start_date = today_local
        end_date = today_local
    elif active_preset == "yesterday":
        start_date = today_local - timedelta(days=1)
        end_date = start_date
    elif active_preset == "last7":
        end_date = today_local
        start_date = today_local - timedelta(days=6)
    elif active_preset == "this_month":
        start_date = today_local.replace(day=1)
        end_date = today_local
    elif active_preset == "all":
        start_date = None
        end_date = None
        sale_day = ""
    elif start_date and not end_date:
        end_date = start_date
    elif end_date and not start_date:
        start_date = end_date

    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    if start_date and end_date:
        start_utc, _ = local_day_bounds(start_date, current_user.timezone_name)
        _, end_utc = local_day_bounds(end_date, current_user.timezone_name)
        statement = statement.where(Sale.created_at >= start_utc, Sale.created_at < end_utc)

    sales = db.scalars(statement).unique().all()
    payment_methods = get_accessible_payment_methods(db, current_user)
    services, _ = get_accessible_services_and_packages(db, current_user)
    history_dashboard = build_history_dashboard(sales, current_user.timezone_name, start_date=start_date, end_date=end_date)

    return templates.TemplateResponse(
        "history.html",
        {
            "sales": [sale_card_payload(sale) for sale in sales],
            "history_dashboard": history_dashboard,
            "timezone_name": current_user.timezone_name,
            "payment_methods": payment_methods,
            "services": services,
            "filters": {
                "q": q or "",
                "payment_method_id": payment_method_filter,
                "service_id": service_filter,
                "preset": active_preset,
                "date_from": start_date.isoformat() if start_date else "",
                "date_to": end_date.isoformat() if end_date else "",
                "sale_day": sale_day or "",
            },
            **layout_context(request, current_user),
        },
    )


@app.get("/wipe-data")
def wipe_data_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    sales_count = len(db.scalars(select(Sale.id).where(Sale.operator_id == current_user.id)).all())
    return templates.TemplateResponse(
        "wipe_data.html",
        {
            "timezone_name": current_user.timezone_name,
            "sales_count": sales_count or 0,
            **layout_context(request, current_user),
        },
    )


@app.post("/wipe-data/range")
def wipe_range(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    start_date: str = Form(...),
    end_date: str = Form(...),
):
    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_day < start_day:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La fecha final debe ser mayor o igual a la inicial.")
    start_utc, _ = local_day_bounds(start_day, current_user.timezone_name)
    _, end_utc = local_day_bounds(end_day, current_user.timezone_name)
    sales = db.scalars(
        select(Sale)
        .options(joinedload(Sale.items))
        .where(Sale.operator_id == current_user.id, Sale.created_at >= start_utc, Sale.created_at < end_utc)
    ).unique().all()
    for sale in sales:
        db.delete(sale)
    db.commit()
    return RedirectResponse("/wipe-data", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/wipe-data/reset")
def wipe_all_data(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    confirmation_text: str = Form(...),
):
    if confirmation_text.strip().upper() != "BORRAR TODO":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Debes escribir "BORRAR TODO" para confirmar.')
    sales = db.scalars(select(Sale).options(joinedload(Sale.items)).where(Sale.operator_id == current_user.id)).unique().all()
    for sale in sales:
        db.delete(sale)
    db.commit()
    return RedirectResponse("/wipe-data", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/profile")
def profile_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    return templates.TemplateResponse("profile.html", build_profile_context(request, db, current_user))


@app.post("/profile")
def update_profile(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    full_name: str = Form(...),
    email: str = Form(...),
    timezone_name: str = Form(...),
    current_password: str = Form(""),
    pabilo_user_bank_id: str = Form(""),
    pabilo_api_key: str = Form(""),
    avatar_file: Optional[UploadFile] = File(None),
):
    normalized_full_name = full_name.strip()
    normalized_email = email.strip().lower()
    normalized_timezone = timezone_name if timezone_name in SUPPORTED_TIMEZONES else current_user.timezone_name
    normalized_pabilo_user_bank_id = pabilo_user_bank_id.strip() or None
    normalized_pabilo_api_key = pabilo_api_key.strip() or None
    pabilo_changed = current_user.is_admin and (
        normalized_pabilo_user_bank_id != (current_user.pabilo_user_bank_id or None)
        or normalized_pabilo_api_key != (current_user.pabilo_api_key or None)
    )
    profile_changed = (
        normalized_full_name != current_user.full_name
        or normalized_email != current_user.email
        or normalized_timezone != current_user.timezone_name
        or pabilo_changed
    )

    if profile_changed and not verify_password(current_password, current_user.password_hash):
        return templates.TemplateResponse(
            "profile.html",
            build_profile_context(request, db, current_user, profile_error="Debes confirmar tu contraseña actual para cambiar datos sensibles."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    current_user.full_name = normalized_full_name
    current_user.email = normalized_email
    current_user.timezone_name = normalized_timezone
    if current_user.is_admin:
        current_user.pabilo_user_bank_id = normalized_pabilo_user_bank_id
        current_user.pabilo_api_key = normalized_pabilo_api_key
    if avatar_file and avatar_file.filename:
        try:
            current_user.avatar_url = save_avatar_upload(avatar_file, current_user.id, current_user.avatar_url)
        except HTTPException as exc:
            return templates.TemplateResponse(
                "profile.html",
                build_profile_context(request, db, current_user, profile_error=str(exc.detail)),
                status_code=exc.status_code,
            )
    db.commit()
    return RedirectResponse("/profile", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/profile/password")
def update_password(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    if not verify_password(current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La contraseña actual no coincide.")
    current_user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/profile", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/profile/request-days")
def request_more_days(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    requested_days: int = Form(...),
    message: str = Form("Necesito extender mi acceso"),
):
    db.add(DaysExtensionRequest(user_id=current_user.id, requested_days=requested_days, message=message.strip() or "Necesito extender mi acceso"))
    db.commit()
    return RedirectResponse("/profile", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/guide")
def guide_page(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
):
    return templates.TemplateResponse("guide.html", layout_context(request, current_user))


@app.get("/admin")
def admin_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_admin_user)],
):
    status_key = request.query_params.get("status")
    target_user_id = request.query_params.get("target_user_id")
    setting = get_setting(db)
    users = db.scalars(select(User).where(User.is_admin.is_(False)).order_by(User.created_at.desc())).all()
    requests = db.scalars(select(DaysExtensionRequest).options(joinedload(DaysExtensionRequest.user)).order_by(DaysExtensionRequest.created_at.desc())).all()
    retention_since = datetime.now(timezone.utc) - timedelta(days=setting.history_retention_months * 31)
    sales = db.scalars(
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method), joinedload(Sale.operator))
        .where(Sale.created_at >= retention_since)
        .order_by(Sale.created_at.desc())
    ).unique().all()
    admin_sales = [sale for sale in sales if not sale.operator.is_admin]
    user_sales_map: dict[int, list[dict]] = {}
    for sale in admin_sales:
        if sale.operator.is_admin:
            continue
        user_sales_map.setdefault(sale.operator_id, []).append(sale_card_payload(sale))

    user_rows = []
    for user in users:
        user_rows.append(
            {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "timezone_name": user.timezone_name,
                "is_active": user.is_active,
                "pabilo_enabled": user.pabilo_enabled,
                "days_left": calculate_days_left(user.subscription_ends_at, user.timezone_name),
                "recent_sales": user_sales_map.get(user.id, [])[:3],
            }
        )

    status_messages = {
        "user-updated": ("success", "Los datos del usuario se actualizaron correctamente."),
        "password-reset": ("success", "La contraseña del usuario se actualizó correctamente."),
        "duplicate-username": ("error", "Ese nombre de usuario ya está en uso por otra cuenta."),
        "duplicate-email": ("error", "Ese correo ya está en uso por otra cuenta."),
        "password-mismatch": ("error", "La nueva contraseña y su confirmación no coinciden."),
        "password-too-short": ("error", "La nueva contraseña debe tener al menos 6 caracteres."),
    }
    admin_feedback = None
    if status_key in status_messages:
        feedback_type, feedback_message = status_messages[status_key]
        admin_feedback = {
            "type": feedback_type,
            "message": feedback_message,
            "target_user_id": int(target_user_id) if target_user_id and target_user_id.isdigit() else None,
        }

    return templates.TemplateResponse(
        "admin.html",
        {
            "admin_users": user_rows,
            "extension_requests": requests,
            "admin_feedback": admin_feedback,
            "recent_sales_summary": build_recent_sales_summary(admin_sales, current_user.timezone_name),
            **layout_context(request, current_user),
        },
    )


@app.post("/admin/users/{user_id}/update")
def update_user_by_admin(
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_admin_user)],
    full_name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    timezone_name: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    pabilo_enabled: bool = Form(False),
):
    user = db.get(User, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    normalized_username = username.strip()
    normalized_email = email.strip().lower()
    normalized_full_name = full_name.strip()
    normalized_timezone = timezone_name if timezone_name in SUPPORTED_TIMEZONES else "America/Caracas"
    password_value = new_password.strip()
    confirm_value = confirm_password.strip()

    username_owner = db.scalar(select(User).where(func.lower(User.username) == normalized_username.lower(), User.id != user.id))
    if username_owner:
        return RedirectResponse(f"/admin?status=duplicate-username&target_user_id={user.id}", status_code=status.HTTP_303_SEE_OTHER)

    email_owner = db.scalar(select(User).where(func.lower(User.email) == normalized_email, User.id != user.id))
    if email_owner:
        return RedirectResponse(f"/admin?status=duplicate-email&target_user_id={user.id}", status_code=status.HTTP_303_SEE_OTHER)

    if password_value or confirm_value:
        if len(password_value) < 6:
            return RedirectResponse(f"/admin?status=password-too-short&target_user_id={user.id}", status_code=status.HTTP_303_SEE_OTHER)
        if password_value != confirm_value:
            return RedirectResponse(f"/admin?status=password-mismatch&target_user_id={user.id}", status_code=status.HTTP_303_SEE_OTHER)

    user.full_name = normalized_full_name
    user.username = normalized_username
    user.email = normalized_email
    user.timezone_name = normalized_timezone
    user.pabilo_enabled = pabilo_enabled

    status_value = "user-updated"
    if password_value:
        user.password_hash = hash_password(password_value)
        status_value = "password-reset"

    db.commit()
    return RedirectResponse(f"/admin?status={status_value}&target_user_id={user.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users/{user_id}/toggle-active")
def toggle_user_active(
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_admin_user)],
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")
    user.is_active = not user.is_active
    db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/requests/{request_id}/review")
def review_extension_request(
    request_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[User, Depends(get_admin_user)],
    status_value: str = Form(...),
):
    extension_request = db.get(DaysExtensionRequest, request_id)
    if not extension_request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Solicitud no encontrada.")
    extension_request.status = status_value
    extension_request.reviewed_at = datetime.now(timezone.utc)
    extension_request.reviewed_by_id = admin_user.id
    if status_value == "approved" and extension_request.requested_days:
        apply_extension_days(extension_request.user, extension_request.requested_days)
    db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/reference-check")
def reference_check(
    payment_method_id: int,
    reference: str,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    payment_method = db.scalar(select(PaymentMethod).where(PaymentMethod.id == payment_method_id, PaymentMethod.is_active.is_(True)))
    if not payment_method:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Método de pago no válido.")
    accessible_method_ids = {method.id for method in get_accessible_payment_methods(db, current_user)}
    if payment_method.id not in accessible_method_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No puedes validar referencias con ese método.")
    return JSONResponse(check_duplicate_reference(db, payment_method_id, reference, current_user.timezone_name))


@app.post("/api/pabilo/verify-reference")
def verify_reference_with_pabilo(
    payload: PabiloReferencePayload,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    if not user_can_verify_pabilo(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu usuario no tiene habilitada la verificación con Pabilo.")

    owner_user = get_pabilo_credentials_owner(db, current_user)
    if not owner_user or not owner_user.pabilo_api_key or not owner_user.pabilo_user_bank_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un administrador debe configurar API Key y UserBankId de Pabilo en su perfil.")

    result = verify_pabilo_reference(
        api_key=owner_user.pabilo_api_key,
        user_bank_id=owner_user.pabilo_user_bank_id,
        reference=payload.reference,
        base_url=settings.pabilo_base_url,
        timeout=settings.pabilo_timeout,
    )
    return JSONResponse(result)


@app.post("/api/sales")
def create_sale(
    payload: CreateSalePayload,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    digits = extract_digits(payload.reference)
    if not digits:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La referencia debe contener números.")

    days_left = calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name)
    allowed_payment_methods = get_accessible_payment_methods(db, current_user)
    _, allowed_package_ids = get_accessible_services_and_packages(db, current_user)
    if not user_can_operate(current_user, days_left, allowed_payment_methods, allowed_package_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu cuenta necesita días activos y accesos asignados para registrar ventas.")

    payment_method_statement = select(PaymentMethod).where(PaymentMethod.id == payload.payment_method_id, PaymentMethod.is_active.is_(True))
    payment_method_statement = apply_catalog_owner_filter(payment_method_statement, PaymentMethod.owner_user_id, current_user)
    payment_method = db.scalar(payment_method_statement)
    if not payment_method:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Método de pago no válido.")

    package_ids = [item.package_id for item in payload.items]
    package_statement = (
        select(Package)
        .options(joinedload(Package.service))
        .join(Package.service)
        .where(
            Package.id.in_(package_ids),
            Package.is_active.is_(True),
            Service.is_active.is_(True),
            Service.payment_method_id == payment_method.id,
        )
    )
    package_statement = apply_catalog_owner_filter(package_statement, Service.owner_user_id, current_user)
    packages = db.scalars(package_statement).unique().all()
    package_map = {package.id: package for package in packages}
    if len(package_map) != len(set(package_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uno o más paquetes no existen.")

    setting = get_setting(db)
    exchange_rate = get_effective_exchange_rate(current_user, setting)
    price_breakdowns = {package.id: package_price_breakdown(package, exchange_rate) for package in packages}
    expected_total_usd = money(sum(price_breakdowns[item.package_id]["usd"] for item in payload.items))
    expected_total_bs = money(sum(price_breakdowns[item.package_id]["bs"] for item in payload.items))

    if payload.amount_paid_value is None or not payload.amount_paid_currency:
        amount_paid_currency = payment_method.currency_code
        if amount_paid_currency == "BS":
            amount_paid_value = expected_total_bs
            amount_paid_usd = expected_total_usd
            amount_paid_bs = expected_total_bs
        else:
            amount_paid_value = expected_total_usd
            amount_paid_usd = expected_total_usd
            amount_paid_bs = expected_total_bs
    else:
        amount_paid_value = money(payload.amount_paid_value)
        amount_paid_currency = normalize_currency_code(payload.amount_paid_currency)
        if amount_paid_currency == "USD":
            amount_paid_usd = amount_paid_value
            amount_paid_bs = money(amount_paid_value * exchange_rate)
        else:
            amount_paid_bs = amount_paid_value
            amount_paid_usd = money(amount_paid_value / exchange_rate)

    duplicate_status = check_duplicate_reference(db, payment_method.id, payload.reference, current_user.timezone_name)
    validation_digits_used = min(len(digits), 6)
    validation_key = build_suffix(digits, 6)
    if duplicate_status["duplicate"]:
        if payload.force_seven_validation:
            if not duplicate_status["can_validate_with_7"]:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede validar esta referencia con 7 dígitos.")
            validation_digits_used = min(len(digits), 7)
            validation_key = build_suffix(digits, 7)
        else:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Referencia duplicada detectada. Usa la validación de 7 dígitos si corresponde.")

    sale = Sale(
        validation_month=current_month_bucket(current_user.timezone_name),
        operator_timezone=current_user.timezone_name,
        reference_raw=payload.reference,
        reference_digits=digits,
        reference_last_6=build_suffix(digits, 6),
        reference_last_7=build_suffix(digits, 7) if len(digits) >= 7 else None,
        validation_key=validation_key,
        validation_digits_used=validation_digits_used,
        amount_paid_value=amount_paid_value,
        amount_paid_currency=amount_paid_currency,
        amount_paid_usd=amount_paid_usd,
        amount_paid_bs=amount_paid_bs,
        expected_total_usd=expected_total_usd,
        expected_total_bs=expected_total_bs,
        payment_method_id=payment_method.id,
        operator_id=current_user.id,
        notes=payload.notes,
    )
    recalculate_sale_exchange_totals(sale, exchange_rate)
    db.add(sale)
    db.flush()

    for item in payload.items:
        package = package_map[item.package_id]
        db.add(
            SaleItem(
                sale_id=sale.id,
                service_id=package.service.id,
                package_id=package.id,
                service_name_snapshot=package.service.name,
                package_name_snapshot=package.name,
                usd_price=price_breakdowns[package.id]["usd"],
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La base de datos bloqueó una referencia duplicada para este mes.")

    created_sale = db.scalar(
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method), joinedload(Sale.operator))
        .where(Sale.id == sale.id)
    )
    return JSONResponse({"ok": True, "sale": sale_card_payload(created_sale or sale)})