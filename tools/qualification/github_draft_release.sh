#!/usr/bin/env bash
# Helpers for a draft GitHub Release. GitHub's tag endpoint deliberately does
# not expose drafts, so release assets must be addressed by immutable ID until
# the final publication step.
set -euo pipefail

ep_draft_release_id() {
  local ids=()
  while IFS= read -r id; do ids+=("$id"); done < <(
    gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" --jq \
      '.[] | select(.draft == true and .tag_name == env.EP_RELEASE_TAG and .target_commitish == env.EP_RELEASE_SOURCE_SHA and .name == env.EP_RELEASE_TITLE) | .id'
  )
  test "${#ids[@]}" -eq 1 || { echo "Expected exactly one matching durable draft release; found ${#ids[@]}." >&2; return 1; }
  printf '%s\n' "${ids[0]}"
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
  local release_id="$1" input="$2" asset_name="$3"
  gh api --hostname uploads.github.com --method POST -H 'Content-Type: application/octet-stream' \
    "repos/$GITHUB_REPOSITORY/releases/$release_id/assets?name=$asset_name" --input "$input" >/dev/null
}

ep_publish_draft() {
  local release_id="$1"
  test "$(gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$release_id" -f draft=false --jq .draft)" = false
}
