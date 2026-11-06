# Apache + mod_wsgi + Flask video web service.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UPLOAD_DIR=/app/uploads

# Apache, plus headers and a compiler to build mod_wsgi for this image's Python.
# curl is used by the healthcheck and for `docker compose exec web curl http://localhost/`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2 apache2-dev gcc libc6-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt \
    # LoadModule line for the mod_wsgi built against /usr/local/bin/python3.12
    && mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi_express.load \
    && a2enmod wsgi_express \
    # Build tools are no longer needed at runtime.
    && apt-get purge -y --auto-remove apache2-dev gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY apache/video-service.conf /etc/apache2/sites-available/video-service.conf
RUN echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf \
    && a2enconf servername \
    && a2dissite 000-default \
    && a2ensite video-service \
    # Apache's global logs (incl. Python logging of the mod_wsgi daemon) -> docker compose logs
    && ln -sf /proc/self/fd/2 /var/log/apache2/error.log \
    && ln -sf /proc/self/fd/1 /var/log/apache2/access.log \
    && ln -sf /proc/self/fd/1 /var/log/apache2/other_vhosts_access.log

COPY web_service.py web_service.wsgi video_client.py ./
COPY tests ./tests

# The named volume "uploads" copies this ownership when it is created.
RUN mkdir -p /app/uploads && chown www-data:www-data /app/uploads

EXPOSE 80

CMD ["apache2ctl", "-D", "FOREGROUND"]
