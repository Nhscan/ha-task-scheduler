ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-aiohttp \
    py3-yaml \
    tzdata

WORKDIR /opt/task-scheduler

COPY rootfs /

RUN pip3 install --no-cache-dir --break-system-packages \
    aiohttp \
    apscheduler \
    python-dateutil

RUN chmod a+x /etc/services.d/task-scheduler/run \
    && chmod a+x /etc/services.d/task-scheduler/finish

LABEL \
    io.hass.name="Task Scheduler Pro" \
    io.hass.description="Timer-based task scheduling with beautiful UI" \
    io.hass.type="addon" \
    io.hass.version="1.0.2"
