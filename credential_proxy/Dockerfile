FROM python:3.12-alpine@sha256:78098ea6a3a9c6a7727a5d4674e4a44e57e01fac878ee9cb4d24a86bd93916ff
WORKDIR /app
COPY credential_proxy/__init__.py credential_proxy/config.py credential_proxy/span_bridge.py credential_proxy/stream_relay.py credential_proxy/http_projection.py /app/credential_proxy/
RUN mkdir -p /run/devc2/spans \
 && chown -R 1000:1000 /app
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "credential_proxy.span_bridge"]
