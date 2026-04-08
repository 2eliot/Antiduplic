# Antiduplic

Aplicación web en Python para registrar ventas y bloquear pagos duplicados por referencia, preparada para SQLite en desarrollo y PostgreSQL en VPS/producción.

## Stack

- FastAPI
- SQLAlchemy
- SQLite para desarrollo
- PostgreSQL para producción
- Jinja2 + JavaScript vanilla

## Requisito de Python

- Python 3.9 o superior.
- En VPS Linux antiguos, `python3` puede apuntar a 3.6; en ese caso instala `python3.9`, `python39`, `python3.10` o `python3.11` y exporta `PYTHON_BIN` con el binario correcto antes de correr `deploy/bootstrap_vps.sh`.

## Funcionalidades implementadas

- Inicio de sesión con sesión de usuario.
- Dashboard operativo optimizado para pantallas angostas.
- Métodos de pago y servicios con opción predeterminada.
- Referencias de cualquier longitud, mostrando los últimos 6 dígitos.
- Detección automática de duplicados al escribir la referencia.
- Validación especial con 7 dígitos cuando existe una colisión real del mes en curso.
- Carrito multi-servicio y multi-paquete.
- Resumen del pedido con totales en USD y Bs según tasa configurable.
- Histórico de operaciones de los últimos 3 meses.
- Perfil con foto, nombre, correo, zona horaria y cambio de contraseña con validación de clave actual.
- Solicitud de extensión de días al administrador.

## Configuración por variables

Todas las claves, credenciales y parámetros de despliegue salen de variables de entorno.

1. Copia `.env.example` a `.env`.
2. Completa al menos estas variables antes de producción:

```env
APP_ENV=production
DATABASE_URL=postgresql+pg8000://usuario:clave@127.0.0.1:5432/antiduplic
SECRET_KEY=un-valor-largo-y-aleatorio
SEED_DEMO_DATA=false
INITIAL_ADMIN_PASSWORD=una-clave-fuerte
```

3. Para desarrollo local puedes usar SQLite con una configuración como esta:

```env
APP_ENV=development
DATABASE_URL=sqlite:///./antiduplic.db
SECRET_KEY=dev-change-me
SEED_DEMO_DATA=true
INITIAL_ADMIN_PASSWORD=cambia-esta-clave-dev
SESSION_HTTPS_ONLY=false
```

## Ejecutar localmente

1. Prepara tu `.env` para desarrollo.
2. Para desarrollo local no necesitas levantar base de datos adicional si usas SQLite. La app creará `antiduplic.db` automáticamente.

3. Instala dependencias:

```powershell
c:/Users/user/Documents/Antiduplic/.venv/Scripts/python.exe -m pip install -r requirements.txt
```

4. Inicia la app:

```powershell
c:/Users/user/Documents/Antiduplic/.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

5. Abre `http://127.0.0.1:8000`.

## PostgreSQL local opcional

Si quieres probar con PostgreSQL local, completa `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` y levanta el contenedor:

```powershell
docker compose up -d
```

Luego apunta `DATABASE_URL` a esas variables.

## Despliegue en VPS

El proyecto incluye artefactos para VPS Linux en la carpeta `deploy/`:

- `deploy/gunicorn.conf.py`: configuración de Gunicorn basada en variables.
- `deploy/antiduplic.service`: unidad base de `systemd` para levantar la app con `.env`.

Flujo recomendado:

1. Crear usuario y carpeta del proyecto en el VPS, por ejemplo `/opt/antiduplic`.
2. Clonar el repo y crear el entorno virtual.
3. Confirmar que el VPS tenga Python 3.9+.
4. Copiar `.env.example` a `.env` y reemplazar todos los placeholders.
5. Instalar dependencias con `pip install -r requirements.txt`.
6. Si usarás PostgreSQL en Docker, completar las variables `POSTGRES_*` y ejecutar `docker compose up -d`.
7. Ajustar `deploy/antiduplic.service` si tu usuario o ruta difieren.
8. Copiar la unidad a `/etc/systemd/system/antiduplic.service` y habilitarla.

Recomendaciones de seguridad para VPS:

- Mantén `SEED_DEMO_DATA=false` después de la instalación inicial.
- Usa un `SECRET_KEY` aleatorio y largo.
- Usa `SESSION_HTTPS_ONLY=true` detrás de HTTPS.
- No subas `.env` al repositorio.
- No reutilices la clave de Postgres como clave de la app.

## Notas de negocio aplicadas

- La validación automática considera solo el mes en curso.
- El histórico conserva 3 meses.
- Si la referencia tiene menos de 6 dígitos, se acepta y se valida con la longitud real disponible.
- Si existe choque real por últimos 6 dígitos, puede aprobarse una segunda operación usando 7 dígitos, siempre que siga siendo única.
