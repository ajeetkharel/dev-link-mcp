#!/bin/sh
# Install dev-link and everything it needs in one go:
#
#   curl -fsSL https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/scripts/install.sh | sh
#
# On Linux: bubblewrap, git and ripgrep from the system package manager (with sudo), and the
# tunnel programs as single binaries in ~/.local/bin. On macOS: git, ripgrep and the tunnel
# programs from Homebrew. Then uv if it is missing, dev-link itself, and Chromium for the browser
# tools. Running it again upgrades dev-link. DEV_LINK_PACKAGE overrides what `uv tool install`
# installs, e.g. a local checkout.
#
# It asks which tunnel program web clients should use: ngrok (the default: your fixed ngrok domain,
# so the URL stays the same), cloudflared (no account, a new URL every start), both, or none. For
# ngrok it also asks for the authtoken and the domain, and writes the domain to the user config.
# Without a terminal it installs cloudflared only, unless DEV_LINK_INSTALL_TUNNEL (cloudflared, ngrok, both, none),
# NGROK_AUTHTOKEN and DEV_LINK_TUNNEL__NGROK_URL say otherwise.
set -eu

# Everything runs from main, so `curl | sh` has read the whole script before any command can
# read from stdin.
main() {
    package="${DEV_LINK_PACKAGE:-dev-link-mcp[all] @ git+https://github.com/ajeetkharel/dev-link-mcp}"
    bin="$HOME/.local/bin"
    os="$(uname -s)"

    if [ "$(id -u)" -eq 0 ]; then
        sudo=""
    elif have sudo; then
        sudo="sudo"
    else
        sudo="none"
    fi

    case "$os" in
        Linux | Darwin) ;;
        *) fail "dev-link runs on Linux and macOS, not $os" ;;
    esac
    # Ask before the long installs, so nobody has to wait around for the questions.
    choose_tunnels
    if [ "$want_ngrok" = 1 ]; then
        ask_ngrok
    fi
    if [ "$os" = Linux ]; then
        install_linux_packages
        if [ "$want_cloudflared" = 1 ]; then install_cloudflared_linux; fi
        if [ "$want_ngrok" = 1 ]; then install_ngrok_linux; fi
    else
        install_macos_packages
    fi

    if ! have uv; then
        say "installing uv"
        curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="$bin:$PATH"

    say "installing dev-link"
    # uv would pick the newest Python, which can be newer than the wheels of compiled
    # dependencies such as tree-sitter; then it builds them and needs a C compiler. CI tests 3.13.
    uv tool install --upgrade --python 3.13 "$package"

    say "installing Chromium for the browser tools"
    dev-link install-browser
    if [ "$os" = Linux ] && have apt-get && ! chromium_launches; then
        # Chromium needs system libraries that Playwright knows how to install on Debian and Ubuntu.
        # Run as you, not root: the tool's Python is user-writable, and Playwright itself puts only
        # its apt-get commands under sudo.
        "$(uv tool dir)/dev-link-mcp/bin/python" -m playwright install-deps chromium ||
            warn "could not install Chromium's system libraries; the browser tools may not start"
    fi

    ngrok_url=""
    if [ "$want_ngrok" = 1 ]; then
        setup_ngrok
    fi

    if [ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ] &&
        [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = 1 ]; then
        warn "AppArmor blocks the user namespaces bubblewrap needs; see" \
            "https://github.com/ajeetkharel/dev-link-mcp/blob/main/CONTRIBUTING.md#setup"
    fi

    say "checking the setup"
    # doctor refuses the home directory as a workspace and then skips the sandbox checks, so
    # check against an empty folder, with a state dir that is removed again.
    probe="$(mktemp -d)"
    probe_state="$(mktemp -d)"
    dev-link doctor "$probe" --state-dir "$probe_state" || true
    rm -rf "$probe" "$probe_state"

    case ":$PATH_BEFORE:" in
        *":$bin:"*) ;;
        *)
            uv tool update-shell >/dev/null 2>&1 || true
            warn "$bin is not on your PATH yet; open a new terminal, or run: export PATH=\"$bin:\$PATH\""
            ;;
    esac
    printf '\nDone. Start a server in your project folder:\n\n  cd your-project && dev-link start --tunnel\n\n'
    if [ -n "$ngrok_url" ]; then
        printf -- '--tunnel publishes on %s, and the URL stays the same across restarts.\n' "$ngrok_url"
    fi
    printf 'Drop --tunnel for a client on this machine (Claude Code, Codex, Cursor, VS Code).\n'
}

# Sets want_cloudflared and want_ngrok from DEV_LINK_INSTALL_TUNNEL or a question on the terminal
# (default ngrok); with neither, cloudflared only.
choose_tunnels() {
    choice="${DEV_LINK_INSTALL_TUNNEL:-}"
    if [ -z "$choice" ] && tty_ok; then
        printf '\nWeb clients such as ChatGPT and Grok reach dev-link through a tunnel. Which one?\n' >/dev/tty
        printf '  1) ngrok (recommended): free account, the URL stays the same, add it to ChatGPT or Grok once\n' >/dev/tty
        printf '  2) cloudflared: no account, but a new URL on every start, so you add it again each time\n' >/dev/tty
        printf '  3) both\n  4) none: only clients on this machine\n' >/dev/tty
        answer="$(ask 'Choice [1]: ')"
        case "$answer" in
            "" | 1) choice=ngrok ;;
            2) choice=cloudflared ;;
            3) choice=both ;;
            4) choice=none ;;
            *) fail "no such choice: $answer" ;;
        esac
    fi
    # Without a terminal there is nobody to give the ngrok authtoken and domain, so cloudflared,
    # which needs neither, is the default there.
    case "${choice:-cloudflared}" in
        cloudflared) want_cloudflared=1 want_ngrok=0 ;;
        ngrok) want_cloudflared=0 want_ngrok=1 ;;
        both) want_cloudflared=1 want_ngrok=1 ;;
        none) want_cloudflared=0 want_ngrok=0 ;;
        *) fail "DEV_LINK_INSTALL_TUNNEL must be cloudflared, ngrok, both or none, not $choice" ;;
    esac
}

# Sets token and url from NGROK_AUTHTOKEN, DEV_LINK_TUNNEL__NGROK_URL or questions on the
# terminal. Either may stay empty, which keeps what is already configured.
ask_ngrok() {
    token="${NGROK_AUTHTOKEN:-}"
    url="${DEV_LINK_TUNNEL__NGROK_URL:-}"
    if ! tty_ok; then
        return
    fi
    if [ -z "$token" ]; then
        printf '\nngrok needs your authtoken, from https://dashboard.ngrok.com/get-started/your-authtoken\n' >/dev/tty
        printf 'If ngrok on this machine already has one, press Enter to skip.\n' >/dev/tty
        ask_secret 'Authtoken (hidden as you type): '
        token="$reply"
    fi
    if [ -z "$url" ]; then
        printf '\nYour ngrok domain, from https://dashboard.ngrok.com/domains\n' >/dev/tty
        url="$(ask 'Domain, e.g. https://name.ngrok-free.app (Enter to set it later): ')"
    fi
}

# Adds the ngrok authtoken and writes the ngrok domain to the user config, from the answers
# ask_ngrok collected; sets ngrok_url when the domain is configured.
setup_ngrok() {
    if ! have ngrok; then
        warn "ngrok is not installed, so it was not set up"
        return
    fi
    if [ -n "$token" ]; then
        ngrok config add-authtoken "$token" >/dev/null || warn "ngrok did not accept the authtoken"
    fi
    if [ -z "$url" ]; then
        warn "no ngrok domain given; set tunnel.ngrok_url in $(user_config) to publish on it"
        return
    fi
    url="$(printf '%s' "${url%/}" | tr '[:upper:]' '[:lower:]')"
    case "$url" in
        https://*) ;;
        *) url="https://$url" ;;
    esac
    case "${url#https://}" in
        "" | *[!A-Za-z0-9.-]*)
            warn "$url is not an ngrok domain like https://name.ngrok-free.app; set tunnel.ngrok_url yourself"
            return
            ;;
    esac
    file="$(user_config)"
    if [ ! -f "$file" ]; then
        dev-link config init >/dev/null
    fi
    if grep -qx "  ngrok_url: $url" "$file"; then
        ngrok_url="$url"
        return
    fi
    if grep -q '^tunnel:' "$file"; then
        warn "$file already has a tunnel section; set tunnel.ngrok_url: $url there yourself"
        return
    fi
    printf '\ntunnel:\n  ngrok_url: %s\n' "$url" >>"$file"
    say "wrote tunnel.ngrok_url to $file"
    ngrok_url="$url"
}

# The path default_user_file() in the dev-link config loader uses.
user_config() {
    printf '%s/dev-link/config.yaml' "${XDG_CONFIG_HOME:-$HOME/.config}"
}

install_linux_packages() {
    if have bwrap && have git && have rg; then
        return
    fi
    say "installing bubblewrap, git and ripgrep"
    if have apt-get; then
        as_root apt-get update -qq
        as_root apt-get install -y -qq bubblewrap git ripgrep
    elif have dnf; then
        as_root dnf install -y -q bubblewrap git ripgrep
    elif have pacman; then
        as_root pacman -S --needed --noconfirm bubblewrap git ripgrep
    elif have zypper; then
        as_root zypper --non-interactive install bubblewrap git ripgrep
    else
        warn "no known package manager; install bubblewrap, git and ripgrep yourself"
    fi
}

install_cloudflared_linux() {
    if have cloudflared; then
        return
    fi
    case "$(uname -m)" in
        x86_64 | amd64) arch=amd64 ;;
        aarch64 | arm64) arch=arm64 ;;
        armv7l | armv6l) arch=arm ;;
        i386 | i686) arch=386 ;;
        *)
            warn "no cloudflared build for $(uname -m); install it yourself to use --tunnel"
            return
            ;;
    esac
    say "installing cloudflared to $bin"
    mkdir -p "$bin"
    curl --proto '=https' --tlsv1.2 -fsSL -o "$bin/cloudflared" \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
    chmod +x "$bin/cloudflared"
}

install_ngrok_linux() {
    if have ngrok; then
        return
    fi
    case "$(uname -m)" in
        x86_64 | amd64) arch=amd64 ;;
        aarch64 | arm64) arch=arm64 ;;
        armv7l | armv6l) arch=arm ;;
        i386 | i686) arch=386 ;;
        *)
            warn "no ngrok build for $(uname -m); install it yourself to use tunnel.ngrok_url"
            return
            ;;
    esac
    say "installing ngrok to $bin"
    mkdir -p "$bin"
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-linux-$arch.tgz" | tar -xzf - -C "$bin" ngrok
    chmod +x "$bin/ngrok"
}

install_macos_packages() {
    have brew || fail "install Homebrew first (https://brew.sh), then run this again"
    packages="git ripgrep"
    if [ "$want_cloudflared" = 1 ]; then packages="$packages cloudflared"; fi
    if [ "$want_ngrok" = 1 ]; then packages="$packages ngrok"; fi
    say "installing $packages"
    # Word splitting is wanted here: one package per word.
    # shellcheck disable=SC2086
    brew install $packages
    warn "macOS has no bubblewrap: start dev-link with --no-sandbox, which gives the agent your full user access"
}

# Chromium only starts once its system libraries are there, so a clean launch means a rerun can
# skip install-deps and its sudo prompt.
chromium_launches() {
    "$(uv tool dir)/dev-link-mcp/bin/python" -c '
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
' >/dev/null 2>&1
}

as_root() {
    if [ "$sudo" = none ]; then
        fail "need root to run: $*; run this script as root or install sudo"
    fi
    $sudo "$@"
}

have() { command -v "$1" >/dev/null 2>&1; }
# `curl | sh` feeds the script on stdin, so questions go through the terminal itself.
tty_ok() { (: </dev/tty) 2>/dev/null; }
ask() {
    printf '%s' "$1" >/dev/tty
    read -r reply </dev/tty || reply=""
    printf '%s' "$reply"
}
# Sets reply rather than printing it: in a $(...) subshell the trap would not run before the
# parent shell dies on Ctrl+C, and the terminal would be left without echo.
ask_secret() {
    printf '%s' "$1" >/dev/tty
    trap 'stty echo </dev/tty 2>/dev/null; exit 130' INT TERM HUP
    stty -echo </dev/tty 2>/dev/null || true
    read -r reply </dev/tty || reply=""
    stty echo </dev/tty 2>/dev/null || true
    trap - INT TERM HUP
    printf '\n' >/dev/tty
}
say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

PATH_BEFORE="$PATH"
main "$@"
