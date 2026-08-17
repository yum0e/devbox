#!/bin/sh
set -eu

readonly upstream=/usr/local/share/pnpm/bin/pi-upstream
readonly openai=/run/devc2/bin/openai

case "${1:-}" in
  -h|--help|-v|--version)
    exec "$upstream" "$@"
    ;;
esac

if [ "${DEVC2_PI_OPENAI_SCOPE:-}" = 1 ]; then
  unset DEVC2_PI_OPENAI_SCOPE
  access="$(jq -er '.tokens.access_token | select(type == "string" and length > 0)' "$TACT_AUTH_FILE")" || {
    echo "pi: the OpenAI Span did not project a usable subscription" >&2
    exit 125
  }
  exec "$upstream" \
    --provider openai-codex \
    --model gpt-5.5 \
    --api-key "$access" \
    "$@"
fi

if [ ! -x "$openai" ]; then
  echo "pi: the OpenAI Span is not granted; restart with --span openai" >&2
  exit 1
fi

exec "$openai" run -- env DEVC2_PI_OPENAI_SCOPE=1 /usr/local/share/pnpm/bin/pi "$@"
