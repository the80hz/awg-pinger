FROM python:3.12-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared


FROM python:3.12-slim AS awg-tools-builder

ARG AMNEZIAWG_TOOLS_REF=v1.0.20260223

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        gcc \
        git \
        libc6-dev \
        make \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${AMNEZIAWG_TOOLS_REF}" \
    https://github.com/amnezia-vpn/amneziawg-tools.git /src/amneziawg-tools

RUN make -C /src/amneziawg-tools/src install \
    DESTDIR=/out \
    WITH_BASHCOMPLETION=no \
    WITH_WGQUICK=yes \
    WITH_SYSTEMDUNITS=no


FROM python-base AS api-side

COPY api_side ./api_side

EXPOSE 8000

CMD ["uvicorn", "api_side.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM python-base AS awg-client-side

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        iptables \
        iproute2 \
        iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=awg-tools-builder /out/usr/bin/awg /usr/bin/awg
COPY --from=awg-tools-builder /out/usr/bin/awg-quick /usr/bin/awg-quick
RUN chmod +x /usr/bin/awg /usr/bin/awg-quick

COPY awg_client_side ./awg_client_side

CMD ["python", "-m", "awg_client_side.main"]
