#!/bin/sh
# Install dev-link and everything it needs in one go:
#
#   curl -fsSL https://raw.githubusercontent.com/ajeetkharel/dev-link-mcp/main/scripts/install.sh | sh
#
# On Linux: bubblewrap, git and ripgrep from the system package manager (with sudo), and
# cloudflared as a single binary in ~/.local/bin. On macOS: git, ripgrep and cloudflared from
# Homebrew. Then uv if it is missing, dev-link itself, and Chromium for the browser tools.
# Running it again upgrades dev-link. DEV_LINK_PACKAGE overrides what `uv tool install` installs,
# e.g. a local checkout.
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
        Linux) install_linux_packages; install_cloudflared_linux ;;
        Darwin) install_macos_packages ;;
        *) fail "dev-link runs on Linux and macOS, not $os" ;;
    esac

    if ! have uv; then
        say "installing uv"
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="$bin:$PATH"

    say "installing dev-link"
    # uv would pick the newest Python, which can be newer than the wheels of compiled
    # dependencies such as tree-sitter; then it builds them and needs a C compiler. CI tests 3.13.
    uv tool install --upgrade --python 3.13 "$package"

    say "installing Chromium for the browser tools"
    dev-link install-browser
    if [ "$os" = Linux ] && have apt-get; then
        # Chromium needs system libraries that Playwright knows how to install on Debian and Ubuntu.
        as_root "$(uv tool dir)/dev-link-mcp/bin/python" -m playwright install-deps chromium ||
            warn "could not install Chromium's system libraries; the browser tools may not start"
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
    printf '\nDone. Start a server in your project folder:\n\n  cd your-project && dev-link start . --tunnel\n\n'
    printf 'Drop --tunnel for a client on this machine (Claude Code, Codex, Cursor, VS Code).\n'
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
    curl -fsSL -o "$bin/cloudflared" \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
    chmod +x "$bin/cloudflared"
}

install_macos_packages() {
    have brew || fail "install Homebrew first (https://brew.sh), then run this again"
    say "installing git, ripgrep and cloudflared"
    brew install git ripgrep cloudflared
    warn "macOS has no bubblewrap: start dev-link with --no-sandbox, which gives the agent your full user access"
}

as_root() {
    if [ "$sudo" = none ]; then
        fail "need root to run: $*; run this script as root or install sudo"
    fi
    $sudo "$@"
}

have() { command -v "$1" >/dev/null 2>&1; }
say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

PATH_BEFORE="$PATH"
main "$@"
