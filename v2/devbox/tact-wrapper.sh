#!/usr/bin/env sh
set -eu

case "${1:-}" in
  config|help|--help|-h|--version|-V)
    exec /usr/local/libexec/devc2/tact-upstream "$@"
    ;;
esac
if [ ! -x /run/devc2/bin/openai ]; then
  echo "tact: the OpenAI Span is not granted; restart with --span openai" >&2
  exit 1
fi
exec /run/devc2/bin/openai run -- /usr/local/libexec/devc2/tact-upstream "$@"
