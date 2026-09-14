#!/usr/bin/env sh
# jarvis dependency bootstrapper.
#
# Usage:
#   sh setup.sh --only <component>
#
# Run this reduced helper from a jarvis source checkout. STRICTLY POSIX sh:
# `sh setup.sh` ignores the shebang above and runs under the system sh (dash on
# many Linux distros). No arrays, no [[ ]], no bashisms.

set -eu

# ------------------------------------------------------------- versions ------

# scip-swift auto-rolls to the LATEST release at install time (resolved
# from the GitHub Releases API — a user decision) and is guarded by this floor instead. v0.2.0/v0.2.1
# silently ignore --build-tool xcodebuild (dispatch regression), and the
# fix landed in 0.3.0 (upstream commit 9bcf1688), so the floor is
# inclusive: a hypothetical 0.2.2 cut from the pre-fix branch would
# still be broken. The v0.1.x-era floors (the `index` subcommand,
# code-signing overrides) are subsumed by 0.3.0.
SCIP_SWIFT_MIN_VERSION="0.3.0"
# Moved from the personal `phuongddx` owner to the org. GitHub serves no
# redirect for the old path, so it 404s rather than forwarding -- every
# `--only scip-swift` run failed until this was repointed.
# tests/test_setup_sh.py asserts the owner never drifts back.
SCIP_SWIFT_REPO="jarvis-intelligence/scip-swift"

# scip-java ships one self-contained launcher asset per release: a POSIX sh
# script with an embedded JAR. It runs on any JVM, so unlike scip-swift there
# is no os/arch gating.
#
# The scip-kotlinc plugin inside it is compiled against Kotlin 2.2.0 EXACTLY.
# Kotlin's compiler-plugin API is internal and unstable: 2.1.21 and 2.3.20 fail
# with AbstractMethodError, and even 2.2.20 fails with NoSuchMethodError. If
# this version is bumped, re-check which Kotlin the new release targets.
SCIP_JAVA_VERSION="v0.13.1"
SCIP_JAVA_REPO="scip-code/scip-java"
SCIP_JAVA_KOTLIN="2.2.0"

# ---------------------------------------------------------------- logging ----

log_info() {
	echo "  $1"
}

log_warn() {
	echo "warn: $1" >&2
}

log_error() {
	echo "error: $1" >&2
}

# ------------------------------------------------------ platform detection ---

# Echo the normalized OS name, or exit non-zero if unsupported.
detect_os() {
	os_raw=$(uname -s)
	case "$os_raw" in
	Darwin) echo "darwin" ;;
	Linux) echo "linux" ;;
	*)
		log_error "$os_raw is not supported (macOS and Linux only)"
		return 1
		;;
	esac
}

# Echo the normalized arch name, or exit non-zero if unsupported.
# Linux reports aarch64 where macOS reports arm64; both normalize to arm64.
detect_arch() {
	arch_raw=$(uname -m)
	case "$arch_raw" in
	arm64 | aarch64) echo "arm64" ;;
	x86_64 | amd64) echo "amd64" ;;
	*)
		log_error "$arch_raw is not supported (arm64 and amd64 only)"
		return 1
		;;
	esac
}

# ------------------------------------------------------------ install dir ----

# Where downloaded binaries go. JARVIS_BIN_DIR exists so tests can redirect
# writes away from the real home directory.
bin_dir() {
	if [ -n "${JARVIS_BIN_DIR:-}" ]; then
		echo "$JARVIS_BIN_DIR"
	else
		echo "${HOME}/.jarvis/bin"
	fi
}

ensure_bin_dir() {
	mkdir -p "$(bin_dir)"
}

# Where bash shims go. Keyed to JARVIS_DATA_DIR, NOT JARVIS_BIN_DIR:
# index_cli.py resolves this same path via config.shim_dir() -> data_dir(),
# which only honours JARVIS_DATA_DIR. A shim the reader cannot find is
# worse than no shim at all.
#
# A sibling of bin_dir rather than a subdirectory: bin/ holds pinned binaries
# we downloaded and own, shims/ holds symlinks to system tools we did not.
shim_dir() {
	echo "${JARVIS_DATA_DIR:-${HOME}/.jarvis}/shims"
}

# Echo the shell rc file to modify, or empty if the shell is unrecognized.
shell_rc_path() {
	case "${SHELL:-}" in
	*/zsh) echo "${HOME}/.zshrc" ;;
	*/bash) echo "${HOME}/.bashrc" ;;
	*) echo "" ;;
	esac
}

# Append the bin dir to the user's shell rc, unless it is already on PATH or
# the line is already present. Idempotent.
ensure_on_path() {
	_dir=$(bin_dir)

	# Already active in this environment: nothing to do.
	case ":${PATH}:" in
	*":${_dir}:"*)
		return 0
		;;
	esac

	_rc=$(shell_rc_path)
	if [ -z "$_rc" ]; then
		log_warn "unrecognized shell '${SHELL:-}'; add ${_dir} to PATH yourself"
		return 0
	fi

	# Already written on a previous run: don't duplicate.
	if [ -f "$_rc" ] && grep -qF "$_dir" "$_rc" 2>/dev/null; then
		return 0
	fi

	# SC2016 is intentional here: $PATH must stay LITERAL in the rc file so it
	# expands at shell-startup time. Expanding it now would bake today's PATH
	# permanently into the rc.
	# shellcheck disable=SC2016
	printf '\n# added by jarvis setup\nexport PATH="%s:$PATH"\n' "$_dir" >>"$_rc"
	log_info "added ${_dir} to ${_rc} — run 'exec \$SHELL' or open a new terminal"
}

# -------------------------------------------------------------- download -----

have_cmd() {
	command -v "$1" >/dev/null 2>&1
}

# A dependency counts as present if it is already in our install dir OR
# anywhere on PATH.
#
# The bin_dir check is load-bearing: setup.sh only *appends* its install dir to
# the shell rc, so that dir is not on PATH during the run that creates it, nor
# on any re-run in the same shell. A PATH-only check therefore re-downloads
# every binary on every re-run -- which CI caught.
already_installed() {
	if [ -x "$(bin_dir)/$1" ]; then
		return 0
	fi
	have_cmd "$1"
}

# Echo the sha256 hex digest of a file. macOS ships shasum; Linux sha256sum.
sha256_of() {
	if have_cmd sha256sum; then
		sha256sum "$1" | cut -d' ' -f1
	elif have_cmd shasum; then
		shasum -a 256 "$1" | cut -d' ' -f1
	else
		log_error "neither sha256sum nor shasum found; cannot verify downloads"
		return 1
	fi
}

verify_sha256() {
	_file=$1
	_expected=$2
	_actual=$(sha256_of "$_file") || return 1
	if [ "$_actual" != "$_expected" ]; then
		log_error "checksum mismatch for ${_file}"
		log_error "  expected: ${_expected}"
		log_error "  actual:   ${_actual}"
		return 1
	fi
}

download_to() {
	curl -fsSL --retry 3 -o "$2" "$1"
}

# True when $1 >= $2 compared as numeric major.minor.patch fields.
# Lexicographic comparison gets 0.10.0 vs 0.3.0 wrong ("10" < "3" as
# strings) and sort -V is GNU-only (absent on macOS/BSD), so compare
# field by field with cut. A leading `v` (git tag form) and missing
# trailing fields are tolerated; equal versions count as "greater or
# equal".
version_ge() {
	_v1=$(printf '%s' "$1" | tr -d 'v')
	_v2=$(printf '%s' "$2" | tr -d 'v')
	_i=1
	while [ "$_i" -le 3 ]; do
		_a=$(printf '%s' "$_v1" | cut -d. -f"$_i")
		_b=$(printf '%s' "$_v2" | cut -d. -f"$_i")
		[ -z "$_a" ] && _a=0
		[ -z "$_b" ] && _b=0
		[ "$_a" -gt "$_b" ] && return 0
		[ "$_a" -lt "$_b" ] && return 1
		_i=$((_i + 1))
	done
	return 0
}

# Download a .tar.gz, verify it against an expected sha256 hex digest
# supplied by the caller, extract one member, and install it into bin_dir()
# under dest_name. scip-swift stopped publishing .sha256 sidecars; GitHub's
# API provides the immutable, server-computed asset digest instead.
#
#   install_tarball_binary_with_digest <tar_url> <expected_hex> <member> <dest_name>
install_tarball_binary_with_digest() {
	_tar_url=$1
	_expected=$2
	_member=$3
	_dest_name=$4

	_tmp=$(mktemp -d)
	# Clean up the temp dir on every exit path, including failure.
	# shellcheck disable=SC2064
	trap "rm -rf '$_tmp'" EXIT

	if ! download_to "$_tar_url" "${_tmp}/archive.tar.gz"; then
		log_error "download failed: ${_tar_url}"
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	if ! verify_sha256 "${_tmp}/archive.tar.gz" "$_expected"; then
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	if ! tar -xzf "${_tmp}/archive.tar.gz" -C "$_tmp" "$_member" 2>/dev/null; then
		log_error "could not extract '${_member}' from archive"
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	ensure_bin_dir
	mv "${_tmp}/${_member}" "$(bin_dir)/${_dest_name}"
	chmod +x "$(bin_dir)/${_dest_name}"

	rm -rf "$_tmp"
	trap - EXIT
}


# Download a bare (non-archive) binary plus its .sha256 sidecar, verify it, and
# install it into bin_dir() under dest_name. Separate from
# a bare binary rather than an archive.
#
#   install_raw_binary <url> <sha_url> <dest_name>
install_raw_binary() {
	_url=$1
	_sha_url=$2
	_dest_name=$3

	_tmp=$(mktemp -d)
	# Clean up the temp dir on every exit path, including failure.
	# shellcheck disable=SC2064
	trap "rm -rf '$_tmp'" EXIT

	if ! download_to "$_url" "${_tmp}/binary"; then
		log_error "download failed: ${_url}"
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	if ! download_to "$_sha_url" "${_tmp}/binary.sha256"; then
		log_error "checksum download failed: ${_sha_url}"
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	# Sidecar format is "<digest>  <filename>"; take the first field.
	_expected=$(cut -d' ' -f1 <"${_tmp}/binary.sha256")
	if ! verify_sha256 "${_tmp}/binary" "$_expected"; then
		rm -rf "$_tmp"
		trap - EXIT
		return 1
	fi

	ensure_bin_dir
	mv "${_tmp}/binary" "$(bin_dir)/${_dest_name}"
	chmod +x "$(bin_dir)/${_dest_name}"

	rm -rf "$_tmp"
	trap - EXIT
}

# ------------------------------------------------------------ installers -----

# Overridable so tests can point at fixtures instead of the real filesystem.
BASH_SHIM_CANDIDATES="${BASH_SHIM_CANDIDATES:-/opt/homebrew/bin/bash /usr/local/bin/bash}"

# True when $1 is a bash >= 4.4. Below that, `set -u` plus an empty
# "${arr[@]}" is an error -- which is exactly how scip-java's generated javac
# wrapper dies on macOS's stock bash 3.2.
#
# Parses `bash --version` rather than $BASH_VERSINFO so a test fixture can be a
# plain sh script. First line looks like:
#   GNU bash, version 5.3.15(1)-release (aarch64-apple-darwin25.4.0)
bash_at_least_44() {
	_bin=$1
	[ -n "$_bin" ] || return 1
	[ -x "$_bin" ] || return 1
	_line=$("$_bin" --version 2>/dev/null | head -n 1) || return 1
	_ver=${_line#*version }
	_ver=${_ver%%[!0-9.]*}
	_major=${_ver%%.*}
	_rest=${_ver#*.}
	_minor=${_rest%%.*}
	case "$_major" in '' | *[!0-9]*) return 1 ;; esac
	case "$_minor" in '' | *[!0-9]*) return 1 ;; esac
	if [ "$_major" -gt 4 ]; then return 0; fi
	if [ "$_major" -eq 4 ] && [ "$_minor" -ge 4 ]; then return 0; fi
	return 1
}

# scip-java's generated javac wrapper is `#!/usr/bin/env bash` with `set -eu`
# and an unguarded "${LAUNCHER_ARGS[@]}", so it needs bash >= 4.4 on PATH.
# macOS ships only 3.2, which breaks every Maven-built Java repo. Linux ships
# >= 4.4, so this is a no-op there.
#
# Never runs `brew`: installing a shell is the user's call.
#
# Always symlinks a known-good bash into the shim dir on darwin, even when
# `command -v bash` here is already modern: this is setup.sh's OWN PATH at
# install time, not necessarily the PATH the indexer subprocess inherits
# later (a GUI-launched MCP server, launchd, a stripped-env shell). Baking
# the resolved path into the shim dir removes that PATH-context dependency --
# `_java_indexer_env()` only checks whether the shim file exists on disk.
install_bash_shim() {
	_os=$1
	if [ "$_os" != "darwin" ]; then
		record bash-shim "not needed (linux ships bash >= 4.4)"
		return 0
	fi

	_default=$(command -v bash 2>/dev/null) || _default=""
	if bash_at_least_44 "$_default"; then
		mkdir -p "$(shim_dir)"
		ln -sf "$_default" "$(shim_dir)/bash"
		log_info "bash-shim: linked default bash (${_default})"
		record bash-shim "ok"
		return 0
	fi

	# SC2086 intentional: BASH_SHIM_CANDIDATES is a space-separated list and
	# must word-split. POSIX sh has no arrays, which is why it is a string.
	# shellcheck disable=SC2086
	for _cand in $BASH_SHIM_CANDIDATES; do
		if bash_at_least_44 "$_cand"; then
			mkdir -p "$(shim_dir)"
			ln -sf "$_cand" "$(shim_dir)/bash"
			log_info "bash-shim: linked ${_cand}"
			record bash-shim "ok"
			return 0
		fi
	done

	log_warn "bash-shim: no bash >= 4.4 found. Maven-built Java repos cannot be SCIP-indexed until you run: brew install bash"
	record bash-shim "skipped (run: brew install bash)"
	return 0
}

install_scip_swift() {
	_os=$1
	_arch=$2

	# Swift indexing reads an Xcode-produced IndexStore, so it is inherently
	# macOS-only; and only an arm64 binary is published. Skipping is expected
	# behavior on other platforms, not a failure. This gate stays FIRST so
	# non-macOS hosts (Linux CI legs) exit without touching the API at all.
	if [ "$_os" != "darwin" ] || [ "$_arch" != "arm64" ]; then
		log_info "scip-swift: not available for ${_os}/${_arch} (macOS arm64 only) — skipping"
		return 0
	fi

	# One anonymous API call serves tag + asset URL + checksum: upstream
	# stopped publishing .sha256 sidecars at v0.2.0, and the API asset
	# `digest` is the server-computed, immutable sha256. Constructing the
	# URL from the tag instead is the documented anti-pattern — the asset
	# naming convention already changed once (v0.1.2 -> v0.2.0).
	# SCIP_SWIFT_API_URL is overridable so tests can serve local JSON.
	_api="${SCIP_SWIFT_API_URL:-https://api.github.com/repos/${SCIP_SWIFT_REPO}/releases/latest}"

	_meta=$(mktemp -d)
	# shellcheck disable=SC2064
	trap "rm -rf '$_meta'" EXIT
	if ! download_to "$_api" "${_meta}/latest.json"; then
		log_error "scip-swift: could not fetch release metadata (${_api})"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi
	_json="${_meta}/latest.json"

	# api.github.com returns pretty-printed JSON, so line-oriented
	# sed/grep/awk extraction suffices (dash-safe, no jq dependency).
	_tag=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$_json" | head -1)
	# Digest and URL must come from the SAME asset -- the macOS one. Taking
	# the first "digest" and the first ".tar.gz" URL independently selects
	# whatever asset happens to be listed first: a future linux asset
	# listed first would be downloaded, digest-verified (its own consistent
	# pair!), and installed, failing only later as an exec-format error.
	# Anchor on the asset NAME instead: upstream publishes
	# `scip-swift-<version>.tar.gz` today (the platform was dropped from
	# the name at v0.2.0, macOS arm64-only by implication) and published
	# `...-macos-arm64.tar.gz` before that -- accept exactly those two
	# shapes so any other platform's asset can never be selected.
	_asset_name=$(grep -oE '"name": *"scip-swift-[0-9][0-9.]*(-macos-arm64)?\.tar\.gz"' "$_json" | head -1 | sed 's/^"name": *"//; s/"$//')
	if [ -z "$_asset_name" ]; then
		log_error "scip-swift: no macOS arm64 .tar.gz asset in release ${_tag:-<no tag>} (${_api})"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi
	# Read digest and URL from that asset's own JSON object: awk flips a
	# flag at every "name" line, so the release title's "name" and every
	# sibling asset are excluded, and the pair is correlated by
	# construction (digest/browser_download_url appear only inside asset
	# objects).
	_block=$(awk -v _needle="\"${_asset_name}\"" '
		/"name":/ { _in = index($0, _needle); next }
		_in { print }
	' "$_json")
	_digest=$(printf '%s\n' "$_block" | grep -o '"digest": *"[^"]*"' | head -1 | sed 's/^"digest": *"//; s/"$//')
	_url=$(printf '%s\n' "$_block" | grep -o '"browser_download_url": *"[^"]*\.tar\.gz"' | head -1 | sed 's/^"browser_download_url": *"//; s/"$//')

	if [ -z "$_tag" ] || [ -z "$_digest" ] || [ -z "$_url" ]; then
		log_error "scip-swift: tag, digest, or asset URL missing for ${_asset_name} (${_api})"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi

	# The API response is untrusted network input flowing into shell
	# variables, a curl URL, and version comparisons — shape-validate every
	# field before use. A wildcard case pattern alone accepts
	# "v0.3.0; touch pwned" (the trailing * swallows the payload), so the
	# tag check pairs it with a complement strip: after deleting every
	# character legal in v<digits>.<digits>.<digits>, nothing may remain.
	case "$_tag" in
	v[0-9]*.[0-9]*.[0-9]*) : ;;
	*)
		log_error "scip-swift: release tag is not v<digits>.<digits>.<digits>: ${_tag}"
		rm -rf "$_meta"; trap - EXIT; return 1
		;;
	esac
	_stray=$(printf '%s' "$_tag" | tr -d 'v0123456789.')
	if [ -n "$_stray" ]; then
		log_error "scip-swift: release tag carries unexpected characters: ${_tag}"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi

	case "$_digest" in
	sha256:*) : ;;
	*)
		log_error "scip-swift: asset digest is not sha256-prefixed: ${_digest}"
		rm -rf "$_meta"; trap - EXIT; return 1
		;;
	esac
	_digest_hex=${_digest#sha256:}
	if [ "${#_digest_hex}" -ne 64 ]; then
		log_error "scip-swift: asset digest is not 64 characters: ${_digest}"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi
	case "$_digest_hex" in
	*[!0-9a-f]*)
		log_error "scip-swift: asset digest is not lowercase hex: ${_digest}"
		rm -rf "$_meta"; trap - EXIT; return 1
		;;
	esac

	# file:// exists solely as the test seam;
	# real release assets are always served over https.
	case "$_url" in
	https://* | file://*) : ;;
	*)
		log_error "scip-swift: asset URL is not https: ${_url}"
		rm -rf "$_meta"; trap - EXIT; return 1
		;;
	esac

	if ! version_ge "$_tag" "$SCIP_SWIFT_MIN_VERSION"; then
		log_error "scip-swift: latest release is ${_tag}, below the required ${SCIP_SWIFT_MIN_VERSION}"
		log_error "scip-swift: no good release exists yet — nothing to install"
		rm -rf "$_meta"; trap - EXIT; return 1
	fi

	# Version-gated, not presence-gated: installs auto-roll to latest, so a stale v0.1.2 left on
	# disk must upgrade, not skip. Compare the installed binary against
	# the RESOLVED tag; bin_dir first, PATH fallback.
	if [ "${FORCE:-0}" != "1" ]; then
		_installed=""
		if [ -x "$(bin_dir)/scip-swift" ]; then
			_installed=$("$(bin_dir)/scip-swift" --version 2>/dev/null || true)
		elif have_cmd scip-swift; then
			_installed=$(scip-swift --version 2>/dev/null || true)
		fi
		if [ -n "$_installed" ]; then
			# scip-swift prints "0.3.0 (swift 6.2.4)"; the version is
			# the first space-delimited token.
			_installed=$(printf '%s' "$_installed" | cut -d' ' -f1)
			# Shape-validate that token like _tag above -- minus _tag's
			# mandatory v, because the binary prints "0.3.0" while
			# GitHub tags are "v0.3.0" -- before comparing: version_ge
			# treats a comparison error as equality, so an unparseable
			# token ("name version" format, a warning line printed
			# first, an rc suffix) would count as current, skip the
			# install, and deadlock against the runtime floor (which
			# says "re-run setup.sh" -- which skips again). Unparseable
			# means outdated: clear the token and let the install
			# proceed.
			case "$_installed" in
			v[0-9]*.[0-9]*.[0-9]* | [0-9]*.[0-9]*.[0-9]*) : ;;
			*)
				log_info "scip-swift: installed version unparseable (${_installed}) — reinstalling"
				_installed=""
				;;
			esac
			if [ -n "$_installed" ]; then
				_stray=$(printf '%s' "$_installed" | tr -d 'v0123456789.')
				if [ -n "$_stray" ]; then
					log_info "scip-swift: installed version unparseable (${_installed}) — reinstalling"
					_installed=""
				fi
			fi
			if [ -n "$_installed" ] && version_ge "$_installed" "$_tag"; then
				log_info "scip-swift: ${_installed} already installed (latest is ${_tag}) — skipping"
				rm -rf "$_meta"; trap - EXIT
				return 0
			fi
			if [ -n "$_installed" ]; then
				log_info "scip-swift: upgrading installed ${_installed} to ${_tag}"
			fi
		fi
	fi

	rm -rf "$_meta"
	trap - EXIT

	log_info "scip-swift: installing ${_tag} (digest-verified)"
	if install_tarball_binary_with_digest "$_url" "$_digest_hex" scip-swift scip-swift; then
		log_info "scip-swift: installed"
	else
		log_error "scip-swift: install failed — build from source: https://github.com/${SCIP_SWIFT_REPO}"
		return 1
	fi
}


# scip-typescript and scip-python are plain npm globals whose bin name matches
# the binary, so one helper covers both.
#
#   install_npm_indexer <binary_name> <npm_package>
install_npm_indexer() {
	_bin=$1
	_pkg=$2

	if [ "${FORCE:-0}" != "1" ] && already_installed "$_bin"; then
		log_info "${_bin}: already installed, skipping"
		return 0
	fi

	if ! have_cmd npm; then
		log_warn "${_bin}: npm not found — skipping. Install Node.js, then: npm install -g ${_pkg}"
		return 0
	fi

	log_info "${_bin}: installing via npm"
	if npm install -g "$_pkg" >/dev/null 2>&1; then
		log_info "${_bin}: installed"
	else
		log_error "${_bin}: npm install failed — try manually: npm install -g ${_pkg}"
		return 1
	fi
}

install_scip_typescript() {
	install_npm_indexer scip-typescript @sourcegraph/scip-typescript
}

install_scip_python() {
	install_npm_indexer scip-python @sourcegraph/scip-python
}

# Installs upstream's single-file launcher without an interactive prompt.
install_scip_java() {
	if [ "${FORCE:-0}" != "1" ] && already_installed scip-java; then
		log_info "scip-java: already installed, skipping"
		return 0
	fi

	# The launcher is a JAR bootstrap: without a JVM it cannot run at all.
	# A soft skip with instructions, matching install_npm_indexer's missing-npm
	# branch — not a hard failure that would abort the whole setup run.
	if ! have_cmd java; then
		log_warn "scip-java: java not found — skipping. Install a JDK, then: ./setup.sh --only scip-java"
		return 0
	fi

	_asset="scip-java-${SCIP_JAVA_VERSION}"
	_base="https://github.com/${SCIP_JAVA_REPO}/releases/download/${SCIP_JAVA_VERSION}"

	log_info "scip-java: installing ${SCIP_JAVA_VERSION} (~86MB launcher)"
	if install_raw_binary "${_base}/${_asset}" "${_base}/${_asset}.sha256" scip-java; then
		log_info "scip-java: installed"
		log_info "scip-java: Kotlin repos must use Kotlin ${SCIP_JAVA_KOTLIN} exactly; Java is unrestricted"
	else
		log_error "scip-java: install failed — see https://github.com/${SCIP_JAVA_REPO}"
		return 1
	fi
}

# ------------------------------------------------------------ orchestration --

ONLY=""
FORCE=0
# POSIX: no arrays, so the summary is a newline-delimited string.
SUMMARY=""
EXIT_CODE=0

usage() {
	cat <<'EOF'
Usage: setup.sh [options]

Run this reduced helper from a jarvis source checkout. It installs optional
language indexers that are not bundled with jarvis's Homebrew distribution.
jarvis, scip, zoekt, and universal-ctags are installed by:
brew install jarvis-intelligence/jarvis/jarvis

Options:
  --only <name>   Install just one component. One of:
                  scip-swift, scip-typescript, scip-python,
                  scip-java, bash-shim
  --force         Reinstall even if already present
  --help          Show this message

Environment:
  JARVIS_BIN_DIR   Override the install directory
  JARVIS_DATA_DIR  Override where the bash shim is created (default ~/.jarvis)
EOF
}

parse_args() {
	while [ $# -gt 0 ]; do
		case "$1" in
		--only)
			if [ $# -lt 2 ]; then
				log_error "--only requires a value"
				return 1
			fi
			case "$2" in
			scip-swift | scip-typescript | scip-python | scip-java | bash-shim)
				ONLY=$2
				;;
			*)
				log_error "--only must be one of: scip-swift, scip-typescript, scip-python, scip-java, bash-shim"
				return 1
				;;
			esac
			shift 2
			;;
		--force)
			FORCE=1
			shift
			;;
		--help | -h)
			usage
			exit 0
			;;
		*)
			log_error "unknown option: $1"
			usage >&2
			return 1
			;;
		esac
	done
}

record() {
	SUMMARY="${SUMMARY}$1:$2
"
}

print_summary() {
	echo ""
	echo "summary"
	printf '%s' "$SUMMARY" | while IFS=: read -r _name _status; do
		[ -n "$_name" ] || continue
		printf '  %-16s %s\n' "$_name" "$_status"
	done
}

# Run one installer, isolating failure so a single bad dependency never
# aborts the whole run.
run_one() {
	_name=$1
	shift
	if "$@"; then
		record "$_name" "ok"
	else
		record "$_name" "FAILED"
		EXIT_CODE=1
	fi
}

should_run() {
	[ -z "$ONLY" ] || [ "$ONLY" = "$1" ]
}

# ----------------------------------------------------------------- main ------

main() {
	parse_args "$@" || exit 2

	echo "jarvis setup"
	OS=$(detect_os) || exit 1
	ARCH=$(detect_arch) || exit 1
	log_info "platform: ${OS}/${ARCH}"
	log_info "install dir: $(bin_dir)"
	echo ""

	ensure_bin_dir

	# `if` form rather than `should_run X && run_one …`: unambiguous exit-status
	# semantics under `set -e` across dash and bash-posix.
	if should_run scip-swift; then run_one scip-swift install_scip_swift "$OS" "$ARCH"; fi
	if should_run scip-typescript; then run_one scip-typescript install_scip_typescript; fi
	if should_run scip-python; then run_one scip-python install_scip_python; fi
	if should_run scip-java; then run_one scip-java install_scip_java; fi
	if should_run bash-shim; then install_bash_shim "$OS"; fi

	ensure_on_path
	print_summary
	exit "$EXIT_CODE"
}

# Testability seam: tests source this file with JARVIS_SETUP_SOURCED=1 to
# call individual functions without performing a real install.
if [ "${JARVIS_SETUP_SOURCED:-}" != "1" ]; then
	main "$@"
fi
