#!/usr/bin/env bash
#
# Refuse to raise a stack whose `.env` cannot produce a living api.
#
#   ./scripts/check-env.sh              # check ./.env
#   ENV_FILE=/path/.env ./scripts/check-env.sh
#   ./scripts/check-env.sh --self-test  # show every verdict, including the refusals
#
# Exit 0 = the stack can start. Exit 1 = it cannot, and the message names the
# key, the state it is in, and the command that fixes it.
#
# --- why this exists --------------------------------------------------------
#
# `.env.example` ships `replace-me-with-openssl-rand-hex-32` for
# SECRET_ENCRYPTION_KEY, and atrium checks that key for *shape* in every
# environment (app/settings.py `_prod_sanity` — despite the name, the hex check
# runs before the prod-only branch). A `.env` copied from the example therefore
# boots an api that raises ValidationError, gets restarted by
# `restart: unless-stopped`, and raises it again.
#
# `make up` reports none of that. Measured in this repo on 2026-09-01, on a
# fresh worktree with a straight `cp .env.example .env`:
#
#   make up                  -> exit 0, "Container …-api-1  Started"
#   docker inspect api       -> Status=restarting, RestartCount climbing 5→8
#   curl :$API_HOST_PORT     -> exit 7, connection refused
#
# The defect is the successful-looking report over something already dead, not
# the placeholder. So the guard runs *before* compose, not after.
#
# --- unset, empty and placeholder are three different states ----------------
#
# They are worth separating because atrium answers each differently, and only
# one of the three is survivable. Measured against
# ghcr.io/brendanbank/atrium:0.29.0 by constructing each state and calling
# `Settings()` under the image's own interpreter:
#
#   SECRET_ENCRYPTION_KEY unset       -> ACCEPTED. Falls back to the dev
#                                        default key, all zeros. The stack
#                                        boots and encrypts happily.
#   SECRET_ENCRYPTION_KEY=            -> REFUSED, "decodes to 0 bytes; need 32"
#   SECRET_ENCRYPTION_KEY=replace-me… -> REFUSED, "must be hex-encoded"
#   SECRET_ENCRYPTION_KEY=<64 hex>    -> ACCEPTED
#
# And the state a missing `.env` produces is **not** the survivable one.
# `compose.yaml` names the variable in the api's `environment:` block, so an
# absent key is interpolated to the empty string rather than omitted —
# `docker compose config` renders `SECRET_ENCRYPTION_KEY: ""`. The unset row
# above is unreachable through compose, which is exactly why "the key is
# missing" and "the key is empty" get different messages here: they arrive by
# different routes and a reader told the wrong one looks in the wrong place.
#
# --- what refuses and what only warns ---------------------------------------
#
# Refusals are reserved for values that make the stack *dead*. Warnings are for
# values that are merely insecure while alive, and they become refusals when
# ENVIRONMENT=prod, which is the same layering atrium's own settings use.
#
# That line is drawn from a measurement rather than from taste. Sweeping the 63
# checkouts of this repo on this workstation on 2026-09-01: APP_SECRET_KEY and
# JWT_SECRET were real in 54 and placeholder in 1 (this worktree, deliberately),
# so refusing on them breaks nobody. MYSQL_ROOT_PASSWORD and MYSQL_PASSWORD
# were still the shipped `…-change-me` string in **17 working stacks** — MySQL
# accepts any non-empty password, so those are valid values and refusing on
# them would stop seventeen stacks that are not broken. Eight checkouts had no
# `.env` at all.
set -uo pipefail

SELF="$(cd -P "$(dirname "$0")" && pwd)/$(basename "$0")"
cd -P "$(dirname "$0")/.." || exit 1

ENV_FILE="${ENV_FILE:-.env}"

fails=0
warns=0

fail() { echo "check-env: REFUSED  $*" >&2; fails=$((fails + 1)); }
warn() { echo "check-env: WARN     $*" >&2; warns=$((warns + 1)); }
hint() { echo "           $*" >&2; }

# Deliberately NOT `. "$ENV_FILE"`. Compose tolerates unquoted values with
# spaces (WEBAUTHN_RP_NAME=Atrium DDNS); the shell does not, and sourcing it
# executes the second word. Same reading scripts/smoke.sh and the Makefile use.
#
# env_has and env_get are separate on purpose: `sed -n 's/^KEY=//p'` returns the
# empty string for "the line is missing" and for "the line is there and empty",
# and this script's whole point is that those are different states.
env_has() { grep -q "^$1=" "$ENV_FILE" 2>/dev/null; }
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n1; }

# The one key whose bad value is fatal in every environment, and the reason
# this script exists.
check_encryption_key() {
	local key="SECRET_ENCRYPTION_KEY" v
	if ! env_has "$key"; then
		fail "$key is absent from $ENV_FILE."
		hint "Absent is not unset: compose.yaml names it in the api's environment"
		hint "block, so it reaches the container as an EMPTY string and atrium"
		hint "refuses with \"decodes to 0 bytes\". Add the line:"
		hint "  echo \"$key=\$(openssl rand -hex 32)\" >> $ENV_FILE"
		return
	fi
	v="$(env_get "$key")"
	case "$v" in
	"")
		fail "$key is present but empty in $ENV_FILE."
		hint "atrium refuses this with \"decodes to 0 bytes; need 32\". Fix:"
		hint "  openssl rand -hex 32     # then paste it after $key="
		return
		;;
	replace-me-*)
		fail "$key is still the placeholder shipped in .env.example."
		hint "atrium refuses this with \"must be hex-encoded\" and the api"
		hint "crash-loops behind a successful-looking \`make up\`. Fix:"
		hint "  make env                 # a fresh .env with real values, or"
		hint "  openssl rand -hex 32     # then paste it after $key="
		return
		;;
	esac
	if ! printf '%s' "$v" | grep -qE '^[0-9a-fA-F]+$'; then
		fail "$key is not hex; atrium refuses it with \"must be hex-encoded\"."
		hint "  openssl rand -hex 32     # then paste it after $key="
		return
	fi
	if [ "${#v}" -ne 64 ]; then
		fail "$key is ${#v} hex chars ($((${#v} / 2)) bytes); atrium needs 64 (32 bytes)."
		hint "  openssl rand -hex 32     # then paste it after $key="
		return
	fi
}

# The signing secrets. A placeholder here does not kill the api — atrium only
# compares them against its own dev default, and only when ENVIRONMENT=prod —
# but a shipped constant used as a signing key is a signing key everyone shares,
# and the sweep says refusing costs nobody anything.
check_signing_secret() {
	local key="$1" v
	if ! env_has "$key"; then
		fail "$key is absent from $ENV_FILE — it reaches the container empty."
		hint "  echo \"$key=\$(openssl rand -hex 48)\" >> $ENV_FILE"
		return
	fi
	v="$(env_get "$key")"
	case "$v" in
	"")
		fail "$key is present but empty; the app would sign with an empty key."
		hint "  openssl rand -hex 48     # then paste it after $key="
		;;
	replace-me-*)
		fail "$key is still the placeholder shipped in .env.example."
		hint "This one does not crash the api — it signs with a constant that is"
		hint "public in this repository, which is quieter and worse. Fix:"
		hint "  make env                 # a fresh .env with real values, or"
		hint "  openssl rand -hex 48     # then paste it after $key="
		;;
	esac
}

# Valid values, so not fatal on a dev box — and 17 working stacks on this
# workstation still carry them. In prod they are a default password behind a
# published port.
check_db_password() {
	local key="$1" v
	if ! env_has "$key"; then
		fail "$key is absent from $ENV_FILE; the mysql image will not initialise."
		return
	fi
	v="$(env_get "$key")"
	if [ -z "$v" ]; then
		fail "$key is present but empty; the mysql image refuses to initialise."
		return
	fi
	case "$v" in
	*-change-me)
		if [ "$ENVIRONMENT" = "prod" ]; then
			fail "$key is still the .env.example default, and ENVIRONMENT=prod."
			hint "  openssl rand -hex 16     # then paste it after $key="
		else
			warn "$key is still the .env.example default. Valid, and fine for a dev"
			hint "stack; change it before anything real. Not a refusal on purpose."
		fi
		;;
	esac
}

# --- --self-test: show the guard refusing ------------------------------------
#
# Every red row below differs from the green one by exactly one line of the
# same file, so a refusal cannot be an artefact of some other difference. It
# builds its own `.env`s in a temp directory: no stack, no docker, no network,
# and it never reads or writes the real `.env`.
run_case() {
	local name="$1" want="$2" prog="$3" f="$TMPDIR_ST/case.env" out rc
	if [ -n "$prog" ]; then sed "$prog" "$TMPDIR_ST/good.env" >"$f"; else cp "$TMPDIR_ST/good.env" "$f"; fi
	out="$(ENV_FILE="$f" "$SELF" 2>&1)"
	rc=$?
	ST_N=$((ST_N + 1))
	if [ "$rc" = "$want" ]; then
		printf '  ok    exit %s  %s\n' "$rc" "$name"
	else
		printf '  FAIL  exit %s (wanted %s)  %s\n' "$rc" "$want" "$name"
		ST_BAD=$((ST_BAD + 1))
	fi
	printf '%s\n' "$out" | sed 's/^/          /'
}

self_test() {
	TMPDIR_ST="$(mktemp -d)"
	ST_N=0
	ST_BAD=0

	echo "check-env.sh --self-test  (synthetic .env files in $TMPDIR_ST; no stack, no docker)"
	echo

	{
		echo "ENVIRONMENT=dev"
		echo "APP_SECRET_KEY=$(openssl rand -hex 48)"
		echo "JWT_SECRET=$(openssl rand -hex 48)"
		echo "SECRET_ENCRYPTION_KEY=$(openssl rand -hex 32)"
		echo "MYSQL_ROOT_PASSWORD=$(openssl rand -hex 8)"
		echo "MYSQL_PASSWORD=$(openssl rand -hex 8)"
	} >"$TMPDIR_ST/good.env"

	run_case "VALID" 0 ""
	run_case "SECRET_ENCRYPTION_KEY placeholder" 1 \
		's#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=replace-me-with-openssl-rand-hex-32#'
	run_case "SECRET_ENCRYPTION_KEY present but empty" 1 \
		's#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=#'
	run_case "SECRET_ENCRYPTION_KEY absent" 1 \
		'/^SECRET_ENCRYPTION_KEY=/d'
	run_case "SECRET_ENCRYPTION_KEY not hex" 1 \
		's#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=zzzz#'
	run_case "SECRET_ENCRYPTION_KEY 16 bytes, not 32" 1 \
		's#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=00112233445566778899aabbccddeeff#'
	run_case "JWT_SECRET placeholder" 1 \
		's#^JWT_SECRET=.*#JWT_SECRET=replace-me-with-openssl-rand-hex-48#'
	run_case "APP_SECRET_KEY placeholder" 1 \
		's#^APP_SECRET_KEY=.*#APP_SECRET_KEY=replace-me-with-openssl-rand-hex-48#'
	run_case "MYSQL_PASSWORD default, ENVIRONMENT=dev -> warn, not refuse" 0 \
		's#^MYSQL_PASSWORD=.*#MYSQL_PASSWORD=db-pw-change-me#'
	run_case "MYSQL_PASSWORD default, ENVIRONMENT=prod -> refuse" 1 \
		's#^ENVIRONMENT=.*#ENVIRONMENT=prod#;s#^MYSQL_PASSWORD=.*#MYSQL_PASSWORD=db-pw-change-me#'

	# The missing-file case cannot be made by damaging the good file.
	local out rc
	out="$(ENV_FILE="$TMPDIR_ST/does-not-exist.env" "$SELF" 2>&1)"
	rc=$?
	ST_N=$((ST_N + 1))
	if [ "$rc" = 1 ]; then
		printf '  ok    exit %s  .env absent entirely\n' "$rc"
	else
		printf '  FAIL  exit %s (wanted 1)  .env absent entirely\n' "$rc"
		ST_BAD=$((ST_BAD + 1))
	fi
	printf '%s\n' "$out" | sed 's/^/          /'

	# The shipped example, unedited, is the exact state issue #137 describes.
	# Asserting on the real file rather than a synthetic copy of it means a
	# future edit that quietly puts a working key in .env.example turns this
	# self-test red.
	if [ -f .env.example ]; then
		out="$(ENV_FILE=.env.example "$SELF" 2>&1)"
		rc=$?
		ST_N=$((ST_N + 1))
		if [ "$rc" = 1 ]; then
			printf '  ok    exit %s  the shipped .env.example (must never pass)\n' "$rc"
		else
			printf '  FAIL  exit %s (wanted 1)  .env.example passes — is a real secret committed?\n' "$rc"
			ST_BAD=$((ST_BAD + 1))
		fi
		printf '%s\n' "$out" | sed 's/^/          /'
	fi

	rm -rf "$TMPDIR_ST"
	echo
	echo "check-env.sh --self-test: $((ST_N - ST_BAD))/$ST_N as expected"
	[ "$ST_BAD" -eq 0 ]
}

[ "${1:-}" = "--self-test" ] && {
	self_test
	exit $?
}

if [ ! -f "$ENV_FILE" ]; then
	echo "check-env: REFUSED  $ENV_FILE does not exist." >&2
	hint "\`docker compose up\` would still exit 0: every \${VAR} interpolates to an"
	hint "empty string, the api dies on SECRET_ENCRYPTION_KEY validation, and"
	hint "\`restart: unless-stopped\` hides it behind a green-looking start."
	hint "  make env      # copies .env.example and mints real secrets into it"
	exit 1
fi

ENVIRONMENT="$(env_get ENVIRONMENT)"
ENVIRONMENT="${ENVIRONMENT:-prod}" # compose.yaml's own default for this key

check_encryption_key
check_signing_secret APP_SECRET_KEY
check_signing_secret JWT_SECRET
check_db_password MYSQL_ROOT_PASSWORD
check_db_password MYSQL_PASSWORD

if [ "$fails" -gt 0 ]; then
	echo "check-env: $fails key(s) must be fixed before this stack starts. Nothing was started." >&2
	exit 1
fi

echo "check-env: $ENV_FILE ok (ENVIRONMENT=$ENVIRONMENT, $warns warning(s))"
exit 0
