from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Optional
from zoneinfo import ZoneInfo, available_timezones

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
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
from app.services.duplicates import build_suffix, check_duplicate_reference, current_month_bucket, extract_digits


templates = Jinja2Templates(directory="templates")
SUPPORTED_TIMEZONES = [
    "America/Caracas",
    "America/Bogota",
    "America/Panama",
    "America/Mexico_City",
    "America/Lima",
    "UTC",
]


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


def ensure_database_features() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return

    sqlite_mode = settings.database_url.startswith("sqlite")
    column_map = {
        "users": {
            "is_admin": "INTEGER NOT NULL DEFAULT 0" if sqlite_mode else "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "payment_methods": {
            "owner_user_id": "INTEGER",
            "notes": "VARCHAR(255)",
        },
        "services": {
            "owner_user_id": "INTEGER",
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

    with engine.begin() as connection:
        for table_name, columns in column_map.items():
            if not inspector.has_table(table_name):
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name not in existing_columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_database_features()
    with SessionLocal() as session:
        if settings.seed_demo_data:
            seed_database(session)
        else:
            ensure_initial_admin(session)
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


def user_can_manage_catalog(user: User) -> bool:
    return user.is_admin or calculate_days_left(user.subscription_ends_at, user.timezone_name) > 0


def get_catalog_manager_user(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user_can_manage_catalog(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Necesitas días activos para gestionar tu catálogo.")
    return current_user


def get_managed_payment_methods(db: Session, user: User) -> list[PaymentMethod]:
    statement = select(PaymentMethod).where(PaymentMethod.owner_user_id == user.id).order_by(PaymentMethod.display_order, PaymentMethod.name)
    return db.scalars(statement).all()


def get_managed_services(db: Session, user: User) -> list[Service]:
    statement = select(Service).options(joinedload(Service.packages)).where(Service.owner_user_id == user.id).order_by(Service.display_order, Service.name)
    return db.scalars(statement).unique().all()


def get_managed_payment_method(db: Session, user: User, method_id: int) -> PaymentMethod:
    statement = select(PaymentMethod).where(PaymentMethod.id == method_id, PaymentMethod.owner_user_id == user.id)
    method = db.scalar(statement)
    if not method:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metodo no encontrado.")
    return method


def get_managed_service(db: Session, user: User, service_id: int) -> Service:
    statement = select(Service).where(Service.id == service_id, Service.owner_user_id == user.id)
    service = db.scalar(statement)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")
    return service


def get_managed_package(db: Session, user: User, package_id: int) -> Package:
    statement = select(Package).options(joinedload(Package.service)).where(Package.id == package_id)
    statement = statement.join(Package.service).where(Service.owner_user_id == user.id)
    package = db.scalar(statement)
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paquete no encontrado.")
    return package


def get_accessible_payment_methods(db: Session, user: User) -> list[PaymentMethod]:
    return db.scalars(
        select(PaymentMethod)
        .where(PaymentMethod.owner_user_id == user.id, PaymentMethod.is_active.is_(True))
        .order_by(PaymentMethod.display_order, PaymentMethod.name)
    ).all()


def get_accessible_services_and_packages(db: Session, user: User) -> tuple[list[Service], set[int]]:
    package_ids = set(
        db.scalars(
            select(Package.id)
            .join(Package.service)
            .where(Service.owner_user_id == user.id, Service.is_active.is_(True), Package.is_active.is_(True))
        ).all()
    )
    if not package_ids:
        return [], set()

    services = db.scalars(
        select(Service)
        .options(joinedload(Service.packages))
        .join(Service.packages)
        .where(Service.is_active.is_(True), Package.id.in_(package_ids))
        .order_by(Service.display_order)
    ).unique().all()
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


def user_can_operate(days_left: int, payment_methods: list[PaymentMethod], allowed_package_ids: set[int]) -> bool:
    return days_left > 0 and bool(payment_methods) and bool(allowed_package_ids)


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


def sidebar_context(user: User) -> dict:
    return {
        "user_card": {
            "name": user.full_name,
            "avatar_url": user.avatar_url,
            "days_left": calculate_days_left(user.subscription_ends_at, user.timezone_name),
            "username": user.username,
        }
    }


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
        "operator_username": sale.operator.username,
        "operator_email": sale.operator.email,
        "reference": sale.reference_digits,
        "validation_digits_used": sale.validation_digits_used,
        "amount_paid_usd": f"{Decimal(sale.amount_paid_usd):.2f}",
        "amount_paid_bs": f"{Decimal(sale.amount_paid_bs):.2f}",
        "expected_total_usd": f"{Decimal(sale.expected_total_usd):.2f}",
        "expected_total_bs": f"{Decimal(sale.expected_total_bs):.2f}",
        "primary_service": sale.items[0].service_name_snapshot if sale.items else "-",
        "items": [
            {"service": item.service_name_snapshot, "package": item.package_name_snapshot}
            for item in sale.items
        ],
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
    days_left = calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name)
    can_operate = user_can_operate(days_left, payment_methods, allowed_package_ids)
    recent_sales = db.scalars(
        select(Sale)
        .options(joinedload(Sale.items), joinedload(Sale.payment_method), joinedload(Sale.operator))
        .where(Sale.operator_id == current_user.id)
        .order_by(Sale.created_at.desc())
        .limit(5)
    ).unique().all()

    context = {
        "payment_methods": payment_methods,
        "services": services,
        "exchange_rate": f"{Decimal(setting.exchange_rate_bs):.2f}",
        "catalog_json": json.dumps(serialize_catalog(services, Decimal(setting.exchange_rate_bs), allowed_package_ids if not current_user.is_admin else None)),
        "timezone_name": current_user.timezone_name,
        "days_left": days_left,
        "has_access": bool(payment_methods and allowed_package_ids),
        "can_operate": can_operate,
        "recent_sales": [sale_card_payload(sale) for sale in recent_sales],
        **layout_context(request, current_user),
    }
    return templates.TemplateResponse("dashboard.html", context)


@app.get("/payment-methods")
def payment_methods_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    methods = get_managed_payment_methods(db, current_user)
    return templates.TemplateResponse(
        "payment_methods.html",
        {
            "payment_methods": methods,
            **layout_context(request, current_user),
        },
    )


@app.post("/payment-methods")
def create_payment_method(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    owner_user_id = None if current_user.is_admin else current_user.id
    method = PaymentMethod(name=name.strip(), owner_user_id=owner_user_id, notes=notes.strip() or None, display_order=display_order)
    db.add(method)
    db.flush()
    if is_default:
        set_payment_method_default(db, method.id, owner_user_id)
    db.commit()
    return RedirectResponse("/payment-methods", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/payment-methods/{method_id}/update")
def update_payment_method(
    method_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    method = get_managed_payment_method(db, current_user, method_id)
    method.name = name.strip()
    method.notes = notes.strip() or None
    method.display_order = display_order
    if is_default:
        set_payment_method_default(db, method.id, method.owner_user_id)
    elif method.is_default:
        method.is_default = False
    db.commit()
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


@app.get("/services")
def services_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
):
    setting = get_setting(db)
    services = get_managed_services(db, current_user)
    return templates.TemplateResponse(
        "services.html",
        {
            "services": services,
            "exchange_rate": Decimal(setting.exchange_rate_bs),
            **layout_context(request, current_user),
        },
    )


@app.post("/services")
def create_service(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    owner_user_id = None if current_user.is_admin else current_user.id
    service = Service(name=name.strip(), owner_user_id=owner_user_id, notes=notes.strip() or None, display_order=display_order)
    db.add(service)
    db.flush()
    if is_default:
        set_service_default(db, service.id, owner_user_id)
    db.commit()
    return RedirectResponse("/services", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/services/{service_id}/update")
def update_service(
    service_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_catalog_manager_user)],
    name: str = Form(...),
    notes: str = Form(""),
    display_order: int = Form(0),
    is_default: bool = Form(False),
):
    service = get_managed_service(db, current_user, service_id)
    service.name = name.strip()
    service.notes = notes.strip() or None
    service.display_order = display_order
    if is_default:
        set_service_default(db, service.id, service.owner_user_id)
    elif service.is_default:
        service.is_default = False
    db.commit()
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
    if sale_day:
        selected_day = datetime.strptime(sale_day, "%Y-%m-%d").date()
        start_utc, end_utc = local_day_bounds(selected_day, current_user.timezone_name)
        statement = statement.where(Sale.created_at >= start_utc, Sale.created_at < end_utc)

    sales = db.scalars(statement).unique().all()
    payment_methods = get_accessible_payment_methods(db, current_user)
    services, _ = get_accessible_services_and_packages(db, current_user)

    return templates.TemplateResponse(
        "history.html",
        {
            "sales": [sale_card_payload(sale) for sale in sales],
            "timezone_name": current_user.timezone_name,
            "payment_methods": payment_methods,
            "services": services,
            "filters": {
                "q": q or "",
                "payment_method_id": payment_method_filter,
                "service_id": service_filter,
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
    setting = get_setting(db)
    timezone_options = [timezone_name for timezone_name in SUPPORTED_TIMEZONES if timezone_name in available_timezones()]
    if current_user.timezone_name not in timezone_options:
        timezone_options.insert(0, current_user.timezone_name)

    return templates.TemplateResponse(
        "profile.html",
        {
            "timezone_options": timezone_options,
            "days_left": calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name),
            "support_contact": setting.support_contact,
            "latest_request": db.scalar(select(DaysExtensionRequest).where(DaysExtensionRequest.user_id == current_user.id).order_by(DaysExtensionRequest.created_at.desc())),
            **layout_context(request, current_user),
        },
    )


@app.post("/profile")
def update_profile(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    full_name: str = Form(...),
    email: str = Form(...),
    avatar_url: str = Form(""),
    timezone_name: str = Form(...),
    current_password: str = Form(...),
):
    if not verify_password(current_password, current_user.password_hash):
        return templates.TemplateResponse(
            "profile.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "current_user": current_user,
                "timezone_options": SUPPORTED_TIMEZONES,
                "days_left": calculate_days_left(current_user.subscription_ends_at, current_user.timezone_name),
                "support_contact": get_setting(db).support_contact,
                "profile_error": "Debes confirmar tu contraseña actual para cambiar datos sensibles.",
                **sidebar_context(current_user),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    current_user.full_name = full_name
    current_user.email = email
    current_user.avatar_url = avatar_url or None
    current_user.timezone_name = timezone_name
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
    user_sales_map: dict[int, list[dict]] = {}
    for sale in sales:
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
                "is_active": user.is_active,
                "days_left": calculate_days_left(user.subscription_ends_at, user.timezone_name),
                "recent_sales": user_sales_map.get(user.id, [])[:3],
            }
        )

    return templates.TemplateResponse(
        "admin.html",
        {
            "admin_users": user_rows,
            "extension_requests": requests,
            **layout_context(request, current_user),
        },
    )


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
    if not user_can_operate(days_left, allowed_payment_methods, allowed_package_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu cuenta necesita días activos y accesos asignados para registrar ventas.")

    payment_method_statement = select(PaymentMethod).where(PaymentMethod.id == payload.payment_method_id, PaymentMethod.is_active.is_(True))
    if not current_user.is_admin:
        payment_method_statement = payment_method_statement.where(PaymentMethod.owner_user_id == current_user.id)
    payment_method = db.scalar(payment_method_statement)
    if not payment_method:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Método de pago no válido.")

    package_ids = [item.package_id for item in payload.items]
    package_statement = (
        select(Package)
        .options(joinedload(Package.service))
        .join(Package.service)
        .where(Package.id.in_(package_ids), Package.is_active.is_(True), Service.is_active.is_(True))
    )
    if not current_user.is_admin:
        package_statement = package_statement.where(Service.owner_user_id == current_user.id)
    packages = db.scalars(package_statement).unique().all()
    package_map = {package.id: package for package in packages}
    if len(package_map) != len(package_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uno o más paquetes no existen.")

    setting = get_setting(db)
    exchange_rate = Decimal(setting.exchange_rate_bs)
    price_breakdowns = {package.id: package_price_breakdown(package, exchange_rate) for package in packages}
    expected_total_usd = money(sum(price_breakdowns[item.package_id]["usd"] for item in payload.items))
    expected_total_bs = money(sum(price_breakdowns[item.package_id]["bs"] for item in payload.items))

    if payload.amount_paid_value is None or not payload.amount_paid_currency:
        amount_paid_value = expected_total_usd
        amount_paid_currency = "USD"
        amount_paid_usd = expected_total_usd
        amount_paid_bs = expected_total_bs
    else:
        amount_paid_value = money(payload.amount_paid_value)
        amount_paid_currency = payload.amount_paid_currency
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

    return JSONResponse({"ok": True, "sale": sale_card_payload(sale)})