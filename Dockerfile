FROM ubuntu:24.04
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG RUNTIME_USER="koa"
ARG RUNTIME_USER_UID=4583
ARG APP_HOME="/koa"
ARG DEBIAN_FRONTEND=noninteractive

RUN apt update && \
    apt install -y python3 python3-venv librrd-dev libpython3-dev build-essential && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir /data && \
    mkdir -p $APP_HOME/static && \
    useradd $RUNTIME_USER -u $RUNTIME_USER_UID && \
    usermod $RUNTIME_USER -d $APP_HOME

WORKDIR $APP_HOME

COPY pyproject.toml uv.lock README.md .
ENV VIRTUAL_ENV=$APP_HOME/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN uv sync --frozen --no-dev --no-install-project --no-editable --link-mode=copy

COPY css $APP_HOME/css
COPY js $APP_HOME/js
COPY static/images $APP_HOME/static/images
COPY backend.py \
    index.html \
    LICENSE \
    NOTICE \
    $APP_HOME/

RUN chown -R $RUNTIME_USER:$RUNTIME_USER $APP_HOME && \
    chown -R $RUNTIME_USER:$RUNTIME_USER /data

VOLUME ["/data"]

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8
ENTRYPOINT ["python3", "-u", "./backend.py"]
