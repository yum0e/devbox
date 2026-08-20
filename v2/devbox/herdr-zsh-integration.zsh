# Report the mounted checkout's branch for this Herdr pane while its shell lives.
if [[ -o interactive && "${HERDR_ENV:-}" == 1 && -n "${HERDR_PANE_ID:-}" &&
      -z "${DEVC2_HERDR_GIT_REPORTER_STARTED:-}" ]]; then
  typeset -gx DEVC2_HERDR_GIT_REPORTER_STARTED=1
  /usr/local/libexec/devc2/herdr-git-metadata \
    "$HERDR_PANE_ID" "$$" "$PWD" "${TACT_WORKSPACE:-$PWD}" \
    </dev/null >/dev/null 2>&1 &!
fi
