#!/bin/sh
set -eu

readonly agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
readonly models_file="$agent_dir/models.json"
readonly settings_file="$agent_dir/settings.json"
readonly token_reference='$DEVC2_PI_OPENAI_TOKEN'

mkdir -p "$agent_dir"
exec 9>"$agent_dir/.configuration.lock"
chmod 0600 "$agent_dir/.configuration.lock"
flock -x 9

for file in "$models_file" "$settings_file"; do
  if [ -L "$file" ] || { [ -e "$file" ] && [ ! -f "$file" ]; }; then
    echo "devc2: refusing to replace non-regular Pi configuration: $file" >&2
    exit 1
  fi
done

if ! { [ -f "$models_file" ] && jq -e --arg value "$token_reference" \
  '.providers["openai-codex"].apiKey == $value' "$models_file" >/dev/null 2>&1; }; then
  temporary="$(mktemp "$agent_dir/.models.json.XXXXXX")"
  trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
  if [ -e "$models_file" ]; then
    models_mode="$(stat -c '%a' "$models_file")"
    jq --arg value "$token_reference" '
      if type != "object" then error("models.json must contain an object") else . end
      | .providers = (.providers // {})
      | if (.providers | type) != "object" then error("models.json providers must be an object") else . end
      | if ((.providers["openai-codex"] // {}) | type) != "object"
        then error("models.json openai-codex provider must be an object") else . end
      | .providers["openai-codex"] = ((.providers["openai-codex"] // {}) + {apiKey: $value})
    ' "$models_file" >"$temporary" || {
      echo "devc2: could not configure the OpenAI Span for Pi in $models_file" >&2
      exit 1
    }
  else
    models_mode=600
    jq -n --arg value "$token_reference" \
      '{providers: {"openai-codex": {apiKey: $value}}}' >"$temporary"
  fi
  chmod "$models_mode" "$temporary"
  mv -f -- "$temporary" "$models_file"
  trap - EXIT HUP INT TERM
fi

# Pi defaults to npm for package dependency installation. This image uses the
# standalone pnpm distribution, so select pnpm unless the user chose a command.
if ! { [ -f "$settings_file" ] && jq -e '.npmCommand != null' \
  "$settings_file" >/dev/null 2>&1; }; then
  temporary="$(mktemp "$agent_dir/.settings.json.XXXXXX")"
  trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
  if [ -e "$settings_file" ]; then
    settings_mode="$(stat -c '%a' "$settings_file")"
    jq '
      if type != "object" then error("settings.json must contain an object") else . end
      | .npmCommand = ["pnpm"]
    ' "$settings_file" >"$temporary" || {
      echo "devc2: could not configure pnpm for Pi in $settings_file" >&2
      exit 1
    }
  else
    settings_mode=600
    jq -n '{npmCommand: ["pnpm"]}' >"$temporary"
  fi
  chmod "$settings_mode" "$temporary"
  mv -f -- "$temporary" "$settings_file"
  trap - EXIT HUP INT TERM
fi
