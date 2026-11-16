#!/usr/bin/env bash
# Install this skill folder for Claude Code and Codex by symlink.
#
#   scripts/install.sh                show the plan (dry run: changes nothing)
#   scripts/install.sh --apply        make the links
#   scripts/install.sh --with-cli     also link bin/living-picture into ~/.local/bin
#   scripts/install.sh --uninstall    plan removing every link that points to this folder (add --apply)
#   scripts/install.sh --only LIST    exactly these targets, a comma list of: claude, agents, codex, cli
#
# One canonical copy: every target is a symlink to this folder, so an edit here reaches every
# agent. A real folder, a file or a symlink that points elsewhere is never replaced: it is
# reported and skipped. Honours CLAUDE_CONFIG_DIR and CODEX_HOME. Targets:
#   claude  $CLAUDE_CONFIG_DIR/skills/living-picture  default; skipped when ~/.claude does not exist
#   agents  ~/.agents/skills/living-picture           default; Codex's documented user skills folder,
#           shared with other agents; made when ~/.agents exists, Codex has been used here, or --only names it
#   codex   $CODEX_HOME/skills/living-picture         only with --only codex, for an older Codex that
#           reads only this folder: current Codex reads both, and two links can list the skill twice
#   cli     ~/.local/bin/living-picture               with --with-cli (or --only cli)
# --uninstall looks at every target unless --only narrows it.
set -euo pipefail

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }  # the comment block above
fail() { printf 'install.sh: %s\n' "$1" >&2; exit 2; }

APPLY=0 UNINSTALL=0 WITH_CLI=0 ONLY="" ONLY_GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --with-cli) WITH_CLI=1 ;;
    --only)
      case "${2:-}" in ''|-*) fail "--only needs a comma list of targets: claude, agents, codex, cli" ;; esac
      ONLY="$2"; ONLY_GIVEN=1; shift ;;
    --only=*) ONLY="${1#--only=}"; ONLY_GIVEN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'install.sh: unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# The targets this run looks at. A typo must fail loudly: an unknown name would otherwise do nothing and look like success.
if [ "$ONLY_GIVEN" = 1 ]; then
  [ -n "$ONLY" ] || fail "--only needs a comma list of targets: claude, agents, codex, cli"
  TARGETS=""
  set -f; old_ifs="$IFS"; IFS=,
  for t in $ONLY; do
    case "$t" in
      claude|agents|codex|cli) TARGETS="$TARGETS $t" ;;
      '') fail "--only: empty name in '$ONLY'" ;;
      *) fail "--only: unknown target '$t' (the targets are claude, agents, codex, cli)" ;;
    esac
  done
  IFS="$old_ifs"; set +f
elif [ "$UNINSTALL" = 1 ]; then
  TARGETS="claude agents codex cli"  # removal only ever touches links to this folder, so look everywhere
else
  TARGETS="claude agents"
fi
if [ "$WITH_CLI" = 1 ]; then TARGETS="$TARGETS cli"; fi
want() { case " $TARGETS " in *" $1 "*) return 0 ;; esac; return 1; }

# This script's real folder, even when it is reached through a symlinked skill folder.
self="$0"
while [ -L "$self" ]; do
  dir="$(cd -P "$(dirname "$self")" && pwd -P)"
  self="$(readlink "$self")"
  case "$self" in /*) ;; *) self="$dir/$self" ;; esac
done
SRC="$(cd -P "$(dirname "$self")/.." && pwd -P)"
NAME="living-picture"
if [ ! -f "$SRC/SKILL.md" ]; then
  printf 'install.sh: %s has no SKILL.md; run the script from inside the skill folder\n' "$SRC" >&2
  exit 2
fi

CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
AGENTS_DIR="$HOME/.agents"
CLI_DIR="$HOME/.local/bin"
CHANGES=0 SKIPPED=0 HERE_AGENTS=0 HERE_CODEX=0

say() { printf '%s\n' "$*"; }

# The physical path of a file or folder, following symlinks; empty when it does not resolve.
real() {
  local p="$1" t n=0
  while [ -L "$p" ] && [ "$n" -lt 40 ]; do
    t="$(readlink "$p")"
    case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac
    n=$((n + 1))
  done
  if [ -d "$p" ]; then
    (cd -P "$p" 2>/dev/null && pwd -P) || true
  elif [ -e "$p" ]; then
    local d
    d="$(cd -P "$(dirname "$p")" 2>/dev/null && pwd -P)" || return 0
    printf '%s/%s\n' "$d" "$(basename "$p")"
  fi
  return 0
}

run() {
  CHANGES=$((CHANGES + 1))
  if [ "$APPLY" = 1 ]; then
    "$@"
  else
    printf '         would run:'
    printf ' %q' "$@"
    printf '\n'
  fi
}

here() {  # remember which Codex folders lead here, to warn about a double listing
  case "$1" in agents) HERE_AGENTS=1 ;; codex) HERE_CODEX=1 ;; esac
}

# link LABEL WHY_NOT DEST TARGET: make DEST a symlink to TARGET unless something is already there.
# WHY_NOT is empty when the agent is set up, otherwise the reason this target is skipped.
link() {
  local label="$1" why_not="$2" dest="$3" target="$4"
  if [ -n "$why_not" ]; then
    say "skip     $label: $why_not; nothing to do"
    return 0
  fi
  if [ -L "$dest" ]; then
    if [ -n "$(real "$dest")" ] && [ "$(real "$dest")" = "$(real "$target")" ]; then
      say "ok       $label: $dest already links here"
      here "$label"
    else
      say "skip     $label: $dest is a symlink to $(readlink "$dest"), not to this folder; left alone"
      SKIPPED=$((SKIPPED + 1))
    fi
    return 0
  fi
  if [ -e "$dest" ]; then
    if [ "$(real "$dest")" = "$(real "$target")" ]; then
      say "ok       $label: $dest is this folder"
      here "$label"
    else
      say "skip     $label: $dest already exists (a real folder or file, not a link here); left alone"
      SKIPPED=$((SKIPPED + 1))
    fi
    return 0
  fi
  say "link     $label: $dest -> $target"
  here "$label"
  [ -d "$(dirname "$dest")" ] || run mkdir -p "$(dirname "$dest")"
  run ln -s "$target" "$dest"
}

# unlink_ours LABEL DEST TARGET: remove DEST only if it is a symlink resolving to TARGET.
unlink_ours() {
  local label="$1" dest="$2" target="$3"
  if [ -L "$dest" ]; then
    if [ -n "$(real "$dest")" ] && [ "$(real "$dest")" = "$(real "$target")" ]; then
      say "remove   $label: $dest (a link here; the folder itself is untouched)"
      run rm "$dest"  # rm without -r on a symlink removes only the link
    else
      say "skip     $label: $dest points to $(readlink "$dest"), not here; left alone"
      SKIPPED=$((SKIPPED + 1))
    fi
  elif [ -e "$dest" ]; then
    say "skip     $label: $dest is not a symlink (perhaps this very folder); left alone"
    SKIPPED=$((SKIPPED + 1))
  else
    say "ok       $label: nothing at $dest"
  fi
}

# Signs that Codex has really been used here. A bare $CODEX_HOME is not one: even `codex --version` creates it.
codex_used=0
if [ -d "$CODEX_DIR/skills" ] || [ -d "$CODEX_DIR/sessions" ] || [ -f "$CODEX_DIR/config.toml" ]; then codex_used=1; fi
why_claude="" why_codex="" why_agents=""
[ -d "$CLAUDE_HOME" ] || why_claude="no $CLAUDE_HOME here (Claude Code not set up)"
[ -d "$CODEX_DIR" ] || why_codex="no $CODEX_DIR here (Codex not set up)"
if [ ! -d "$AGENTS_DIR" ] && [ "$codex_used" = 0 ] && [ "$ONLY_GIVEN" = 0 ]; then
  why_agents="no $AGENTS_DIR and no sign that Codex has been used here (--only agents makes it anyway)"
fi

if [ "$APPLY" = 1 ]; then mode="applying"; else mode="dry run (nothing changes; add --apply to do it)"; fi
say "living-picture install: $SRC"
say "mode: $([ "$UNINSTALL" = 1 ] && printf 'uninstall, ' || true)$mode; targets: $(printf '%s' "${TARGETS# }")"

if [ "$UNINSTALL" = 1 ]; then
  if want claude; then unlink_ours claude "$CLAUDE_HOME/skills/$NAME" "$SRC"; fi
  if want agents; then unlink_ours agents "$AGENTS_DIR/skills/$NAME" "$SRC"; fi
  if want codex; then unlink_ours codex "$CODEX_DIR/skills/$NAME" "$SRC"; fi
  if want cli; then unlink_ours cli "$CLI_DIR/$NAME" "$SRC/bin/$NAME"; fi
else
  if want claude; then link claude "$why_claude" "$CLAUDE_HOME/skills/$NAME" "$SRC"; fi
  if want agents; then link agents "$why_agents" "$AGENTS_DIR/skills/$NAME" "$SRC"; fi
  if want codex; then link codex "$why_codex" "$CODEX_DIR/skills/$NAME" "$SRC"; fi
  if want cli; then
    [ -x "$SRC/bin/$NAME" ] || say "note     cli: $SRC/bin/$NAME is not executable (chmod +x it)"
    link cli "" "$CLI_DIR/$NAME" "$SRC/bin/$NAME"
    case ":$PATH:" in
      *":$CLI_DIR:"*) ;;
      *) say "note     cli: $CLI_DIR is not on PATH; add to ~/.zprofile: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
  fi
  # the other folder may already lead here from an earlier install, so look at both
  if [ "$HERE_AGENTS" = 0 ] && [ -n "$(real "$AGENTS_DIR/skills/$NAME")" ] && [ "$(real "$AGENTS_DIR/skills/$NAME")" = "$SRC" ]; then HERE_AGENTS=1; fi
  if [ "$HERE_CODEX" = 0 ] && [ -n "$(real "$CODEX_DIR/skills/$NAME")" ] && [ "$(real "$CODEX_DIR/skills/$NAME")" = "$SRC" ]; then HERE_CODEX=1; fi
  if [ "$HERE_AGENTS" = 1 ] && [ "$HERE_CODEX" = 1 ]; then
    say "note     Codex reads both $AGENTS_DIR/skills and $CODEX_DIR/skills; with both leading here it may list $NAME twice."
    say "         Keep one: scripts/install.sh --uninstall --only codex --apply"
  fi
fi

if [ "$CHANGES" = 0 ]; then
  say "nothing to change ($SKIPPED skipped)."
elif [ "$APPLY" = 1 ] && [ "$UNINSTALL" = 1 ]; then
  say "done: $CHANGES link(s) removed, $SKIPPED skipped."
elif [ "$APPLY" = 1 ]; then
  say "done: $CHANGES change(s), $SKIPPED skipped. Claude Code: /$NAME (picked up live). Codex: \$$NAME or /skills."
else
  say "$CHANGES change(s) planned, $SKIPPED skipped. Run again with --apply to make them."
fi
