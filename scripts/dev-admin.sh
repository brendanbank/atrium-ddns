#!/usr/bin/env bash
#
# Seed this stack's admin from a 1Password login item.
#
# `make seed-admin EMAIL=… PASSWORD=…` wants both typed on the command
# line. That puts the password in your shell history, in the terminal
# scrollback, and — because make echoes its recipes — in any log of the
# run. It also cannot pre-enrol TOTP, so the account it creates is not the
# one you actually sign in as: you get as far as the password and the
# second factor is not there.
#
# This reads all three out of one 1Password item instead:
#
#     username                            ->  --email
#     password                            ->  --password
#     one-time password (otpauth:// URI)  ->  --totp-secret <base32>
#
# so the account that exists afterwards is the one your authenticator
# already has an entry for. TOTP is optional: an item without an OTP field
# seeds an admin without one, and says so rather than leaving you to find
# out at the login screen.
#
# ## Why the item is not named in this file
#
# The item's title is a hostname, and no real hostname belongs in a tracked
# file. Same rule `.env.example`'s `TRAEFIK_HOSTNAME` follows — placeholder
# in the tracked template, real value supplied locally — and the same
# reason `.overnight.local.conf` sits beside `.overnight.conf`.
#
# So the reference lives in `.devstack.local.conf`, which is gitignored:
#
#     echo 'DDNS_OP_ITEM=<title, UUID or op:// reference>' > .devstack.local.conf
#
# or in the environment, or as the first argument. **Prefer the UUID.** It
# identifies the item exactly, survives a rename, does not depend on which
# vault is default, and is the one spelling that discloses nothing if the
# file is ever pasted somewhere it should not be.
#
# ## Why the values go in over stdin
#
# `docker compose exec -T api python … --password X` puts the password in
# the argv of a process on the *host*, where any local user's `ps` reads it
# — which is what `make seed-admin` does today. Piping into a `read` inside
# the container keeps the host's argv secret-free. The values are still in
# the container's own process table for as long as the seeder runs, which
# is a much smaller room and one the seeder has to be in regardless.
#
#   ./scripts/dev-admin.sh                  # reference from .devstack.local.conf
#   ./scripts/dev-admin.sh <uuid-or-title>  # or say it explicitly
#   ./scripts/dev-admin.sh --check          # resolve everything, seed nothing
#
# `--check` exists so `make dev-up` can refuse *before* it builds an image.
# Learning that the CLI is locked at the end of a five-minute build, with a
# stack already running, is the wrong order to learn it in.
#
# Exit 0 = the admin was seeded. Every refusal names what to do about it.
set -euo pipefail

cd -P "$(dirname "$0")/.." || exit 1

# The compose project this repo's Makefile pins. Left to compose, the name
# comes from the directory you invoked *from* — so a run through a symlink
# to this worktree addresses a different project than the one the stack was
# created under, finds no containers, and reports that as a failure of the
# stack rather than of the lookup. `cd -P` above resolves the symlink; this
# honours an explicit COMPOSE_PROJECT when make exports one.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')}"
compose() { docker compose -p "$COMPOSE_PROJECT" "$@"; }

CONF=.devstack.local.conf

die() { printf '%s\n' "$@" >&2; exit 2; }

# --- Resolve the item reference ---------------------------------------------
#
# Argument, then environment, then the local config. The config is read for
# one key rather than sourced — the same way scripts/smoke.sh reads `.env`,
# and for the same reason: sourcing a config file executes it.
CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then
  CHECK_ONLY=1
  shift
fi

ITEM="${1:-${DDNS_OP_ITEM:-}}"
if [ -z "$ITEM" ] && [ -f "$CONF" ]; then
  ITEM="$(sed -n 's/^DDNS_OP_ITEM=//p' "$CONF" | tail -n1)"
fi

if [ -z "$ITEM" ]; then
  die "No 1Password item to read." \
      "" \
      "Say which item holds this stack's admin login, once:" \
      "" \
      "    echo 'DDNS_OP_ITEM=<uuid>' > $CONF" \
      "" \
      "$CONF is gitignored. Prefer the UUID over the title: it survives a" \
      "rename and discloses nothing. List them with" \
      "" \
      "    op item list --format json | jq -r '.[] | \"\\(.id)  \\(.title)\"'" \
      "" \
      "It can also be the first argument, or DDNS_OP_ITEM in the environment."
fi

# --- The 1Password CLI ------------------------------------------------------
command -v op >/dev/null 2>&1 || die \
  "The 1Password CLI (op) is not installed." \
  "    brew install 1password-cli"

# Checked separately from the fetch below so that "not signed in" cannot
# read as "no such item". `op` exits non-zero for both and the two have
# nothing in common as fixes.
op account list >/dev/null 2>&1 || die \
  "The 1Password CLI is not signed in." \
  "    eval \$(op signin)" \
  "" \
  "If op is installed but has never been unlocked from a terminal, turn on" \
  "'Integrate with 1Password CLI' in the desktop app's Developer settings."

ITEM_JSON="$(op item get "$ITEM" --format json 2>/dev/null)" || die \
  "1Password has no item matching that reference." \
  "" \
  "The reference itself is not echoed here on purpose. Check it against" \
  "" \
  "    op item list --format json | jq -r '.[] | \"\\(.id)  \\(.title)\"'"

# `--check` stops here: the reference resolved, the CLI is unlocked, and
# the item exists. Deliberately *before* the fields are read — a check that
# pulled the password would put it in this shell for no reason.
if [ "$CHECK_ONLY" = "1" ]; then
  echo "1Password: signed in, item resolved"
  exit 0
fi

# --- Pull the three fields --------------------------------------------------
#
# The OTP field is matched on its *type*, not its id: 1Password generates
# that id per item (`TOTP_<random>`), so an id match would work against the
# item it was written for and silently find nothing on the next one — which
# seeds an admin with no second factor and reports success.
#
# `--totp-secret` wants the bare base32 secret, but the field's value is a
# whole `otpauth://` URI, so `secret=` is what gets extracted. Reading it
# with `op item get --otp` instead would hand back the current six-digit
# *code*, which is not a thing that can be enrolled.
#
# Four newline-separated values. NUL would be tidier, but the reader on the
# other end is the container's `/bin/sh` — dash, whose `read` has no `-d` —
# so newline is the separator both sides can actually agree on, and the
# extractor below refuses any value containing one.
FIELDS="$(printf '%s' "$ITEM_JSON" | python3 -c '
import json, sys, urllib.parse

item = json.load(sys.stdin)
fields = item.get("fields", [])


def by_id(want):
    for f in fields:
        if f.get("id") == want:
            return f.get("value") or ""
    return ""


def totp_secret():
    """The base32 secret from the otpauth:// URI, or "" if the item has none."""
    for f in fields:
        if f.get("type") != "OTP":
            continue
        raw = f.get("value") or ""
        if not raw:
            continue
        # Some items store the bare secret rather than a URI. Both are
        # accepted; only the URI needs unwrapping.
        if not raw.startswith("otpauth://"):
            return raw
        query = urllib.parse.urlparse(raw).query
        got = urllib.parse.parse_qs(query).get("secret", [""])[0]
        if got:
            return got
    return ""


email = by_id("username")
password = by_id("password")
totp = totp_secret()
# The title is a hostname, so it is never printed. It is used only as the
# seeded account name — a string inside your own dev database.
full_name = item.get("title") or "Dev Admin"

if not email:
    sys.exit("that item has no username field")
if not password:
    sys.exit("that item has no password field")

# A newline in any of these would desynchronise the reader on the other side
# of the pipe and seed a truncated password. That fails at the login screen,
# later, looking exactly like a typo.
for label, value in (
    ("username", email),
    ("password", password),
    ("OTP secret", totp),
    ("title", full_name),
):
    if "\n" in value or "\r" in value:
        sys.exit(f"the {label} field contains a newline; refusing to seed a truncated value")

sys.stdout.write("\n".join((email, password, totp, full_name)))
')" || die "" "Could not read the credentials out of that item (see above)."

# --- Seed -------------------------------------------------------------------
#
# `set --` builds the argv inside the container so `--totp-secret` is
# omitted entirely when the item has no OTP field, rather than passed
# empty: an empty base32 secret enrols a second factor that can never
# produce a valid code, which is worse than having none.
#
# stdout is dropped because the seeder confirms by printing the address it
# created, and that address is at a real domain. Failures go to stderr and
# are left alone.
printf '%s\n' "$FIELDS" | compose exec -T api sh -c '
  IFS= read -r EMAIL
  IFS= read -r PASSWORD
  IFS= read -r TOTP
  IFS= read -r FULL_NAME
  set -- --email "$EMAIL" --password "$PASSWORD" \
         --full-name "$FULL_NAME" --super-admin
  if [ -n "$TOTP" ]; then
    set -- "$@" --totp-secret "$TOTP"
  fi
  exec python -m app.scripts.seed_admin "$@"
' >/dev/null

# Neither the address nor the item title is printed. What is worth
# confirming is the half you cannot see from the login screen until you are
# already past the password.
if [ -n "$(printf '%s' "$FIELDS" | sed -n '3p')" ]; then
  echo "seeded the admin from 1Password — TOTP enrolled, your authenticator's codes work"
else
  echo "seeded the admin from 1Password — no TOTP (that item has no one-time password field)"
fi
