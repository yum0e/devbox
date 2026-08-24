FROM python:3.12-alpine@sha256:78098ea6a3a9c6a7727a5d4674e4a44e57e01fac878ee9cb4d24a86bd93916ff
WORKDIR /app
COPY span_gateway/__init__.py span_gateway/config.py span_gateway/gateway.py span_gateway/stream_relay.py span_gateway/http_connect_proxy.py /app/span_gateway/
RUN mkdir -p /run/devc2/spans \
 && chown -R 1000:1000 /app
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "span_gateway.gateway"]
