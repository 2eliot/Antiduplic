import os


bind = f"{os.getenv('APP_HOST', '127.0.0.1')}:{os.getenv('APP_PORT', '8000')}"
workers = int(os.getenv("GUNICORN_WORKERS", os.getenv("GUNICORN_WEB_CONCURRENCY", "2")))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
accesslog = "-"
errorlog = "-"
capture_output = True
