#!/usr/bin/env bash
# Helpers for a draft GitHub Release. GitHub's tag endpoint deliberately does
# not expose drafts, so release assets must be addressed by immutable ID until
# the final publication step.
set -euo pipefail

ep_draft_release_id() {
  local ids=()
  while IFS= read -r id; do ids+=("$id"); done < <(
    # `gh api --jq` evaluates gojq without the workflow shell environment, so
    # `env.EP_RELEASE_*` silently selects no draft.  Query the REST collection
    # and select the immutable release ID from the draft's own source and name.
    # A draft release may not yet be addressable by its eventual tag.
    gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" | python3 -c '
import json
import os
import sys

payload = sys.stdin.read()
decoder = json.JSONDecoder()
releases = []
while payload.strip():
    payload = payload.lstrip()
    page, offset = decoder.raw_decode(payload)
    if not isinstance(page, list):
        raise SystemExit("GitHub releases API returned a non-list page")
    releases.extend(page)
    payload = payload[offset:]

for release in releases:
    if (
        release.get("draft") is True
        and release.get("target_commitish") == os.environ["EP_RELEASE_SOURCE_SHA"]
        and release.get("name") == os.environ["EP_RELEASE_TITLE"]
    ):
        print(release["id"])
'
  )
  test "${#ids[@]}" -eq 1 || { echo "Expected exactly one matching durable draft release identity; found ${#ids[@]}." >&2; return 1; }
  printf '%s\n' "${ids[0]}"
}

ep_create_draft_release() {
  # Do not use `gh api --jq` here.  A truncated create response from GitHub
  # is otherwise reduced to its opaque "unexpected end of JSON input" error,
  # despite this being the release operation's durable pre-publication step.
  # Keep the actual GitHub draft ID as the handle while it remains a draft.
  local payload response
  payload="$(python3 -c 'import json, sys; print(json.dumps({"tag_name": sys.argv[1], "target_commitish": sys.argv[2], "name": sys.argv[3], "draft": True}))' \
    "$EP_RELEASE_TAG" "$EP_RELEASE_SOURCE_SHA" "$EP_RELEASE_TITLE")"
  response="$(curl --fail --silent --show-error --request POST \
    -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required for draft release creation}" \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H 'Content-Type: application/json' \
    --data "$payload" \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/releases")"
  printf '%s' "$response" | python3 -c '
import json
import sys

try:
    release = json.load(sys.stdin)
except json.JSONDecodeError as error:
    raise SystemExit(f"draft release creation returned invalid JSON: {error}")
release_id = release.get("id") if isinstance(release, dict) else None
if not isinstance(release_id, int) or release_id <= 0:
    raise SystemExit("draft release creation response has no valid immutable release id")
print(release_id)
'
}

ep_draft_asset_id() {
  local release_id="$1" asset_name="$2" ids=()
  while IFS= read -r id; do ids+=("$id"); done < <(
    gh api "repos/$GITHUB_REPOSITORY/releases/$release_id" --jq ".assets[] | select(.name == \"$asset_name\") | .id"
  )
  test "${#ids[@]}" -eq 1 || { echo "Expected exactly one draft receipt asset named $asset_name; found ${#ids[@]}." >&2; return 1; }
  printf '%s\n' "${ids[0]}"
}

ep_draft_asset_names() {
  gh api "repos/$GITHUB_REPOSITORY/releases/$1" --jq '.assets[].name'
}

ep_draft_download() {
  local release_id="$1" asset_name="$2" output="$3" asset_id
  asset_id="$(ep_draft_asset_id "$release_id" "$asset_name")"
  gh api -H 'Accept: application/octet-stream' "repos/$GITHUB_REPOSITORY/releases/assets/$asset_id" > "$output"
}

ep_draft_upload() {
  local release_id="$1" input="$2" asset_name="$3" encoded_name
  encoded_name="$(python3 -c 'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' "$asset_name")"
  curl --fail --silent --show-error --request POST \
    -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required for draft asset upload}" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary "@$input" \
    "https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets?name=$encoded_name" >/dev/null
}

ep_publish_draft() {
  local release_id="$1"
  test "$(gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$release_id" -f draft=false --jq .draft)" = false
}
