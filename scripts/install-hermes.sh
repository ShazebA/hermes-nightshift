#!/usr/bin/env bash
# Wire Nightshift into a local Hermes Agent install.
#
#   scripts/install-hermes.sh                # print what would change (default, touches nothing)
#   scripts/install-hermes.sh --apply        # copy the skill + cron scripts into $HERMES_HOME
#
# It never creates cron jobs or kanban boards itself; it prints the exact `hermes` commands.
# Options (env): HERMES_HOME (default ~/.hermes), NIGHTSHIFT_CONFIG (default
# ~/.config/nightshift/nightshift.yaml), DELIVER (digest target, default local).
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HH="${HERMES_HOME:-$HOME/.hermes}"
CONFIG="${NIGHTSHIFT_CONFIG:-$HOME/.config/nightshift/nightshift.yaml}"
DELIVER="${DELIVER:-local}"
NS="$(command -v nightshift || true)"
GH="$(command -v gh || true)"
HERMES="$(command -v hermes || true)"

[[ -n "$NS" ]] || { echo "nightshift is not on PATH (uv tool install . or pip install -e .)" >&2; exit 1; }
[[ -n "$GH" ]] || { echo "gh is not on PATH" >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG (copy config.example.yaml)" >&2; exit 1; }
# Cron scripts run with the gateway's environment, which may lack your shell PATH.
PATH_PREFIX="$(dirname "$GH")"
[[ -n "$HERMES" ]] && PATH_PREFIX="$PATH_PREFIX:$(dirname "$HERMES")"

render() {
  sed -e "s|@@NIGHTSHIFT@@|$NS|" -e "s|@@CONFIG@@|$CONFIG|" -e "s|@@PATH_PREFIX@@|$PATH_PREFIX|" "$1"
}

SKILL_DST="$HH/skills/devops/nightshift-triage"
echo "Hermes home:   $HH"
echo "nightshift:    $NS"
echo "config:        $CONFIG"
echo
echo "Files:"
echo "  $ROOT/hermes/skills/nightshift-triage/SKILL.md -> $SKILL_DST/SKILL.md"
echo "  $HH/scripts/nightshift-watch.sh"
echo "  $HH/scripts/nightshift-digest.sh"

if [[ $APPLY == 1 ]]; then
  mkdir -p "$SKILL_DST" "$HH/scripts"
  cp "$ROOT/hermes/skills/nightshift-triage/SKILL.md" "$SKILL_DST/SKILL.md"
  render "$ROOT/hermes/scripts/nightshift-watch.sh.tmpl" > "$HH/scripts/nightshift-watch.sh"
  render "$ROOT/hermes/scripts/nightshift-digest.sh.tmpl" > "$HH/scripts/nightshift-digest.sh"
  chmod +x "$HH/scripts/nightshift-watch.sh" "$HH/scripts/nightshift-digest.sh"
  echo "  (written)"
else
  echo "  (dry run: re-run with --apply to write them)"
fi

cat <<CMDS

Then register the jobs (run these yourself):

  hermes cron create "every 15m" "Follow the nightshift-triage skill for each brief listed above." \\
      --name nightshift-watch --script nightshift-watch.sh --skill nightshift-triage --deliver local

  hermes cron create "0 7 * * *" --name nightshift-digest \\
      --script nightshift-digest.sh --no-agent --deliver $DELIVER

If your model is Hermes's managed llama.cpp runtime (model.provider: llamacpp), pin the watch
job's provider. Otherwise Hermes snapshots it as "custom", sends no key, and the managed
llama-server answers HTTP 401:

  hermes cron edit <nightshift-watch id> --provider llamacpp --model <model.default>

Optional dedicated kanban board (set kanban.board: nightshift in your config):

  hermes kanban boards create nightshift --name "Nightshift" --description "CI/deploy triage briefs"
CMDS
