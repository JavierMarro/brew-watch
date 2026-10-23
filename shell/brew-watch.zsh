# Loaded by the installer's marked .zshrc block, after Homebrew's shell setup.
[[ -o interactive && -o zle && -t 0 && -t 1 ]] || return 0
(( ${+_BREW_WATCH_LOADED} )) && return 0
typeset -g _BREW_WATCH_LOADED=1
typeset -g _BREW_WATCH_OUTPUT=''
zmodload zsh/system || return 0

_brew_watch_ready() {
  emulate -L zsh
  local fd=$1 chunk result
  # Read one available chunk per callback; never wait for a line or for EOF.
  sysread -i "$fd" -s 4096 -t 0 chunk
  result=$?
  if (( result == 0 )); then
    _BREW_WATCH_OUTPUT+=$chunk
    (( ${#_BREW_WATCH_OUTPUT} <= 16384 )) && return 0
    _BREW_WATCH_OUTPUT=$'brew-watch: reminder too large; run brew outdated --verbose.\n'
  elif (( result == 4 )); then
    return 0
  elif (( result != 5 )); then
    _BREW_WATCH_OUTPUT=''
  fi
  zle -F "$fd"
  exec {fd}<&-
  if [[ -n $_BREW_WATCH_OUTPUT ]]; then
    zle -I
    print -rn -- "$_BREW_WATCH_OUTPUT"
    _BREW_WATCH_OUTPUT=''
    zle reset-prompt
  fi
}

_brew_watch_start() {
  emulate -L zsh
  add-zsh-hook -d precmd _brew_watch_start
  [[ -x "$_BREW_WATCH_COMMAND" ]] || return 0
  local fd
  exec {fd}< <("$_BREW_WATCH_COMMAND" _startup </dev/null 2>&1)
  zle -F "$fd" _brew_watch_ready
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _brew_watch_start
