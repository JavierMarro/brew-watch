# Loaded by the installer's marked .zshrc block, after Homebrew's shell setup.
[[ -o interactive && -o zle && -t 0 && -t 1 ]] || return 0
(( ${+_BREW_WATCH_LOADED} )) && return 0
typeset -g _BREW_WATCH_LOADED=1

_brew_watch_ready() {
  emulate -L zsh
  local fd=$1 line
  # Invalidate the displayed input before printing; zle redraws what was typed.
  zle -I
  while IFS= read -r line <&$fd; do
    print -r -- "$line"
  done
  zle -F "$fd"
  exec {fd}<&-
  zle reset-prompt
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
