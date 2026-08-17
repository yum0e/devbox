#!/bin/sh
set -eu

readonly upstream=/usr/local/share/pnpm/bin/pi-upstream
readonly openai=/run/devc2/bin/openai

case "${1:-}" in
  -h|--help|-v|--version)
    exec "$upstream" "$@"
    ;;
esac

if [ ! -x "$openai" ]; then
  echo "pi: the OpenAI Span is not granted; restart with --span openai" >&2
  exit 1
fi

exec "$openai" run -- "$upstream" "$@"
