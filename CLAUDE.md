# Antiduplic — contexto para Claude

Responde siempre en español.

App web FastAPI para registrar ventas y bloquear pagos duplicados por referencia. Ver [README.md](README.md) para detalles completos.

## Stack
- FastAPI + SQLAlchemy 2 + Jinja2 + JS vanilla ([static/app.js](static/app.js))
- SQLite en desarrollo, PostgreSQL (pg8000) en producción/VPS
- Configuración vía `.env` (ver [.env.example](.env.example)), cargada en [app/config.py](app/config.py)

## Estructura
- [app/main.py](app/main.py): rutas y vistas
- [app/models.py](app/models.py): modelos
- [app/services/duplicates.py](app/services/duplicates.py): lógica de detección de duplicados
- [app/services/pabilo.py](app/services/pabilo.py): integración Pabilo
- [templates/](templates/): vistas HTML
- [deploy/](deploy/): scripts y unidad systemd para VPS (ver [deploy/vps-deploy.md](deploy/vps-deploy.md))

## Ejecutar localmente
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --reload-dir app --reload-dir templates --reload-dir static
```
Dev: `.env` local con SQLite y `SEED_DEMO_DATA=true`; login de pruebas `admin` (o `admin@example.com`) / `123456` (el login acepta correo o usuario). En Windows hace falta `tzdata` (ya en requirements).
Abrir http://127.0.0.1:8000

## Reglas de negocio clave
- Validación de duplicados solo en el mes en curso; histórico de 3 meses.
- Se comparan los últimos 6 dígitos de la referencia (o la longitud real si tiene menos).
- Si hay choque real con 6 dígitos, se permite una segunda operación validando con 7 dígitos, siempre que sea única.

## Reglas de tasas y monedas
- Cada venta guarda su tasa en `Sale.exchange_rate_bs`; al editar una venta se usa esa tasa, nunca la tasa actual del usuario.
- Los montos en Bs (paquetes con precio en Bs o montos de Pabilo) se guardan exactos: no convertir Bs→USD→Bs (eso perdía céntimos).
- Editar una venta en el historial solo recalcula lo que cambió (monto/moneda o paquete principal); los demás ítems se conservan.
- Pabilo acepta montos en formato venezolano (1.234,56) e internacional (1,234.56).

## Validaciones del servidor (no confiar solo en el HTML)
- Paquetes: nombre no vacío, moneda USD/BS, precio > 0. Una venta con total 0 se rechaza.
- Solicitudes de días: entre 1 y 365; solo se revisan si están "pending" (no se suman días dos veces).
- Usuario/correo únicos sin distinguir mayúsculas (el login acepta usuario o correo).
- Contraseñas: mínimo 6 caracteres en registro y cambio de contraseña.
- Historial: al editar se puede conservar el paquete/método original aunque esté desactivado.
- El admin no puede desactivarse a sí mismo ni a otros admins.

## Notas de sesiones
(Agrega aquí decisiones o pendientes importantes para recordarlos entre sesiones.)
