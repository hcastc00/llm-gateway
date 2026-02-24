#!/usr/bin/env bash
# LLM Gateway — one-command installer
# Usage: curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/llm-gateway/main/install.sh | bash

set -euo pipefail

REPO_URL="https://github.com/hcastc00/llm-gateway"
INSTALL_DIR="${LLM_GATEWAY_DIR:-$HOME/llm-gateway}"

# ── Colours (only when stdout is a tty) ───────────────────────────────────────
if [[ -t 1 ]]; then
  R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' B='\033[0;34m' W='\033[1m' N='\033[0m'
else
  R='' G='' Y='' B='' W='' N=''
fi

info() { printf "${B}▸${N} %s\n" "$*"; }
ok()   { printf "${G}✓${N} %s\n" "$*"; }
warn() { printf "${Y}⚠${N}  %s\n" "$*"; }
die()  { printf "${R}✗ %s${N}\n" "$*" >&2; exit 1; }
hdr()  { printf "\n${W}%s${N}\n" "$*"; }

# Reconnect stdin to the terminal so interactive prompts work even under curl | bash
[[ -e /dev/tty ]] && exec < /dev/tty

prompt() {
  # Usage: prompt <varname> <question> [default]
  local __var="$1" __q="$2" __def="${3:-}" __val
  if [[ -n "$__def" ]]; then
    printf "${W}%s${N} [%s]: " "$__q" "$__def"
  else
    printf "${W}%s${N}: " "$__q"
  fi
  read -r __val || true
  [[ -z "$__val" && -n "$__def" ]] && __val="$__def"
  while [[ -z "$__val" ]]; do
    printf "${R}Required.${N} ${W}%s${N}: " "$__q"
    read -r __val || true
  done
  printf -v "$__var" '%s' "$__val"
}

# ── Prerequisites ──────────────────────────────────────────────────────────────
get_lan_ip() {
  case "$(uname -s)" in
    Linux)  hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost" ;;
    Darwin) ipconfig getifaddr en0 2>/dev/null || echo "localhost" ;;
    *)      echo "localhost" ;;
  esac
}

check_docker() {
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    ok "Docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
    return
  fi
  hdr "Installing Docker"
  case "$(uname -s)" in
    Linux)
      curl -fsSL https://get.docker.com | sh
      sudo systemctl enable --now docker
      if ! groups "$USER" 2>/dev/null | grep -q docker; then
        sudo usermod -aG docker "$USER"
        warn "Added $USER to the docker group — you may need to log out and back in."
        warn "If the next step fails, run: newgrp docker"
      fi
      ;;
    Darwin)
      die "Please install Docker Desktop from https://docs.docker.com/desktop/install/mac-install/ then re-run."
      ;;
    *)
      die "Unsupported OS. Install Docker manually then re-run."
      ;;
  esac
  ok "Docker installed"
}

check_compose() {
  docker compose version &>/dev/null 2>&1 \
    || die "Docker Compose v2 not found. Update Docker or install the compose plugin."
  ok "Docker Compose $(docker compose version --short 2>/dev/null || echo 'v2')"
}

# ── Repo ──────────────────────────────────────────────────────────────────────
fetch_repo() {
  # Dev mode: running the script from inside the repo already
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)" || script_dir=""
  if [[ -n "$script_dir" && -f "$script_dir/docker-compose.yml" ]]; then
    INSTALL_DIR="$script_dir"
    ok "Using existing repo at $INSTALL_DIR"
    return
  fi

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Found existing install at $INSTALL_DIR — pulling latest"
    git -C "$INSTALL_DIR" pull --ff-only
    ok "Updated"
    return
  fi

  command -v git &>/dev/null || die "git is required. Install it and re-run."
  info "Cloning to $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
  ok "Cloned"
}

# ── Config prompts ─────────────────────────────────────────────────────────────
collect_config() {
  hdr "Configuration"

  prompt UPSTREAM_URL   "llama.cpp upstream URL"              "http://192.168.1.46:8001"
  prompt MAX_CONCURRENT "Max concurrent requests to upstream" "1"

  hdr "API Keys"
  printf "Add at least one API key + username pair.\n"
  printf "These are the Bearer tokens your clients will use.\n\n"

  USERS_JSON="{"
  local first=true key uname
  while true; do
    printf "${W}API key${N} (blank to finish): "
    read -r key || true
    [[ -z "$key" ]] && break
    printf "${W}Username for this key${N}: "
    read -r uname || true
    if [[ -z "$uname" ]]; then warn "Username required, skipping."; continue; fi
    [[ "$first" == true ]] || USERS_JSON+=","
    USERS_JSON+="\"$key\":\"$uname\""
    first=false
    ok "Added: $uname"
  done
  [[ "$first" == true ]] && die "At least one API key is required."
  USERS_JSON+="}"

  hdr "Cloudflare Tunnel"
  cat <<'EOF'

  To get your tunnel token:

    1. Go to https://one.dash.cloudflare.com
    2. Networks → Tunnels → Create a tunnel → Cloudflared
    3. Give it a name (e.g. llm-gateway) and save
    4. Choose the Docker connector tab — copy the token from the
       command shown (the long string after --token)
    5. In the tunnel's "Public Hostname" tab, add:
         Domain:  your.domain.com
         Service: http://gateway:8000

EOF
  prompt TUNNEL_TOKEN "Paste your Cloudflare tunnel token"
}

# ── Write config files ─────────────────────────────────────────────────────────
write_files() {
  local env_file="$INSTALL_DIR/.env"

  if [[ -f "$env_file" ]]; then
    printf "${Y}%s already exists. Overwrite?${N} [y/N]: " "$env_file"
    read -r ans || true
    if [[ "${ans,,}" != "y" ]]; then
      warn "Keeping existing .env — make sure TUNNEL_TOKEN is set inside it."
    else
      write_env "$env_file"
    fi
  else
    write_env "$env_file"
  fi

  printf '%s\n' "$USERS_JSON" > "$INSTALL_DIR/users.json"
  ok "Written users.json"
}

write_env() {
  cat > "$1" <<EOF
UPSTREAM_URL=$UPSTREAM_URL
MAX_CONCURRENT=$MAX_CONCURRENT
TUNNEL_TOKEN=$TUNNEL_TOKEN
EOF
  ok "Written .env"
}

# ── Launch ────────────────────────────────────────────────────────────────────
launch() {
  hdr "Starting LLM Gateway"
  cd "$INSTALL_DIR"

  # Use sudo docker if the user was just added to the docker group
  local dc="docker compose"
  docker info &>/dev/null 2>&1 || dc="sudo docker compose"

  $dc pull cloudflared 2>/dev/null || true
  $dc up -d --build
}

# ── Done ──────────────────────────────────────────────────────────────────────
print_done() {
  local lan_ip
  lan_ip="$(get_lan_ip)"

  printf "\n${G}╔══════════════════════════════╗${N}\n"
  printf   "${G}║   LLM Gateway is running!   ║${N}\n"
  printf   "${G}╚══════════════════════════════╝${N}\n\n"

  printf "  LAN access:  http://%s:8000\n" "$lan_ip"
  printf "  Health:      curl http://localhost:8000/health\n"
  printf "  Metrics:     curl http://localhost:8000/metrics\n\n"
  printf "  Manage (from %s):\n" "$INSTALL_DIR"
  printf "    docker compose logs -f      # stream logs\n"
  printf "    docker compose restart      # restart\n"
  printf "    docker compose down         # stop\n\n"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  printf "\n${W}  LLM Gateway Installer${N}\n"
  printf   "  ──────────────────────\n\n"

  check_docker
  check_compose
  fetch_repo
  collect_config
  write_files
  launch
  print_done
}

main "$@"
