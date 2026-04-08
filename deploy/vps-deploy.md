# Despliegue VPS

Servidor objetivo: `159.198.45.30`

Repo: `https://github.com/2eliot/Antiduplic.git`

Variables que debes decidir antes de ejecutar:

- `SSH_USER`: usuario con acceso SSH al VPS.
- `GIT_REPO_URL`: URL del repo GitHub de Antiduplic.
- `GIT_BRANCH`: rama a desplegar, por ejemplo `main`.

Comandos desde tu máquina local:

```bash
ssh SSH_USER@159.198.45.30 "mkdir -p /opt/antiduplic"
scp -r deploy SSH_USER@159.198.45.30:/tmp/antiduplic-deploy
ssh SSH_USER@159.198.45.30 "cp -r /tmp/antiduplic-deploy/* /opt/antiduplic/deploy || true"
ssh SSH_USER@159.198.45.30 "chmod +x /opt/antiduplic/deploy/*.sh"
ssh SSH_USER@159.198.45.30 "export GIT_REPO_URL='https://github.com/2eliot/Antiduplic.git'; export GIT_BRANCH='main'; bash /opt/antiduplic/deploy/bootstrap_vps.sh"
```

Editar variables sensibles en el VPS:

```bash
ssh SSH_USER@159.198.45.30
nano /opt/antiduplic/.env
```

Variables mínimas sugeridas en `.env` para VPS:

```env
APP_ENV=production
APP_HOST=127.0.0.1
APP_PORT=8000
SESSION_HTTPS_ONLY=true
DATABASE_URL=postgresql+pg8000://antiduplic:TU_DB_PASSWORD@127.0.0.1:5432/antiduplic
SECRET_KEY=TU_SECRET_LARGO_Y_ALEATORIO
SEED_DEMO_DATA=false
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_FULL_NAME=Administrador Antiduplic
INITIAL_ADMIN_EMAIL=admin@tu-dominio.com
INITIAL_ADMIN_PASSWORD=TU_ADMIN_PASSWORD_SEGURO
INITIAL_ADMIN_TIMEZONE=America/Caracas
POSTGRES_DB=antiduplic
POSTGRES_USER=antiduplic
POSTGRES_PASSWORD=TU_DB_PASSWORD
POSTGRES_PORT=5432
POSTGRES_CONTAINER_NAME=antiduplic-postgres
GUNICORN_WORKERS=2
GUNICORN_TIMEOUT=60
```

Levantar Postgres y servicio:

```bash
cd /opt/antiduplic
docker compose up -d
bash deploy/install_service.sh
```

Verificación:

```bash
systemctl status antiduplic --no-pager
docker compose ps
ss -ltnp | grep ':8000\|:5432'
curl -I http://127.0.0.1:8000
```

Actualización posterior:

```bash
ssh SSH_USER@159.198.45.30 "bash /opt/antiduplic/deploy/update_vps.sh"
```