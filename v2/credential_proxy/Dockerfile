FROM python:3.12-alpine
WORKDIR /app
COPY credential_proxy /app/credential_proxy
RUN mkdir -p /run/devc2-ssh && chown -R 1000:1000 /app /run/devc2-ssh && chmod 0777 /run/devc2-ssh
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "credential_proxy.ssh_agent_proxy"]
