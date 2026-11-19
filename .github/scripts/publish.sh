#!/usr/bin/env bash
# Publishes every package in this repo whose version is not on its registry yet:
#   packages/*/package.json  -> npm (trusted publishing: no token, GitHub's OIDC identity)
#   python/*/pyproject.toml  -> PyPI (PYPI_TOKEN)
# A version already published is skipped, so running this again is safe. To release a
# package, bump its version in a pull request; this runs when it merges.
#
# npm: trusted publishing only works for a package that already exists on npm. A brand-new
# @groundbox/* package gets its first version from the main groundbox repo's
# "Publish integrations (first npm version)" workflow; then add this repo as its Trusted
# Publisher on npmjs.com (package → Settings → Trusted Publisher → GitHub Actions,
# Ground-box/groundbox-integrations, workflow publish.yml).
set -uo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
failed=0

publish_npm() {
  local dir="$1" name version
  name="$(node -p "require('$dir/package.json').name")"
  version="$(node -p "require('$dir/package.json').version")"
  if [[ "$(node -p "require('$dir/package.json').private === true")" == "true" ]]; then echo "$name is private, skipped"; return 0; fi
  if [[ -n "$(npm view "$name@$version" version 2>/dev/null)" ]]; then echo "$name $version is already on npm"; return 0; fi
  if [[ -z "$(npm view "$name" name 2>/dev/null)" ]]; then
    echo "::error::$name is not on npm yet. Run the main groundbox repo's 'Publish integrations (first npm version)' workflow, then add this repo as its Trusted Publisher on npmjs.com."
    return 1
  fi
  echo "publishing $name $version"
  (cd "$dir" && npm install --no-audit --no-fund && { npm run --if-present build; } && npm publish --access public --provenance) || { echo "::error::$name: publish failed"; return 1; }
}

publish_pypi() {
  local dir="$1" name version status out
  name="$(python3 -c "import tomllib,sys;print(tomllib.load(open('$dir/pyproject.toml','rb'))['project']['name'])")"
  version="$(python3 -c "import tomllib,sys;print(tomllib.load(open('$dir/pyproject.toml','rb'))['project']['version'])")"
  status="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$name/$version/json")"
  if [[ "$status" == "200" ]]; then echo "$name $version is already on PyPI"; return 0; fi
  if [[ "$status" != "404" ]]; then echo "::error::PyPI answered $status for $name $version"; return 1; fi
  if [[ -z "${PYPI_TOKEN:-}" ]]; then echo "::error::$name $version is not on PyPI and PYPI_TOKEN is not set"; return 1; fi
  echo "publishing $name $version to PyPI"
  out="$(mktemp -d)"
  python3 -m build --outdir "$out" "$dir" || { echo "::error::$name: build failed"; return 1; }
  TWINE_USERNAME=__token__ TWINE_PASSWORD="$PYPI_TOKEN" python3 -m twine upload --verbose --non-interactive --disable-progress-bar "$out"/* || { echo "::error::$name: upload failed"; return 1; }
}

shopt -s nullglob
for f in "$root"/packages/*/package.json; do publish_npm "$(dirname "$f")" || failed=1; done
python3 -m pip install --quiet build twine
for f in "$root"/python/*/pyproject.toml; do publish_pypi "$(dirname "$f")" || failed=1; done
exit "$failed"
