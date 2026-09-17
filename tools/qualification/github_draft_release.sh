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

ep_draft_asset_records() {
  local release_id="$1" asset_name="$2"
  gh api "repos/$GITHUB_REPOSITORY/releases/$release_id" | ASSET_NAME="$asset_name" python3 -c '
import json
import os
import sys

release = json.load(sys.stdin)
assets = release.get("assets") if isinstance(release, dict) else None
if not isinstance(assets, list):
    raise SystemExit("GitHub release response has no asset list")
for asset in assets:
    if isinstance(asset, dict) and asset.get("name") == os.environ["ASSET_NAME"]:
        asset_id, state = asset.get("id"), asset.get("state")
        if not isinstance(asset_id, int) or asset_id <= 0 or not isinstance(state, str) or not state:
            raise SystemExit("matching GitHub release asset has invalid identity or state")
        print(f"{asset_id}\t{state}")
'
}

ep_draft_upload() {
  local release_id="$1" input="$2" asset_name="$3" encoded_name
  local attempt=1 max_attempts=6 curl_status http_status records asset_id asset_state
  local matching=()
  encoded_name="$(python3 -c 'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' "$asset_name")"
  while [ "$attempt" -le "$max_attempts" ]; do
    set +e
    http_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
      --request POST \
      -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required for draft asset upload}" \
      -H 'Accept: application/vnd.github+json' \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      -H 'Content-Type: application/octet-stream' \
      --data-binary "@$input" \
      "https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets?name=$encoded_name")"
    curl_status=$?
    set -e
    if [ "$curl_status" -eq 0 ] && [ "$http_status" = 201 ]; then
      return 0
    fi

    # Authentication, authorization and identity failures are permanent and
    # must never cause another mutating request.
    if [[ "$http_status" =~ ^4[0-9][0-9]$ ]] && [ "$http_status" != 408 ] && [ "$http_status" != 422 ] && [ "$http_status" != 429 ]; then
      echo "GitHub draft asset upload failed permanently with HTTP $http_status." >&2
      return 1
    fi

    # A transport error, 5xx or duplicate response is ambiguous: GitHub may
    # already have accepted the bytes.  Perform an authoritative read before
    # deciding whether any further mutation is safe.
    if ! records="$(ep_draft_asset_records "$release_id" "$asset_name")"; then
      echo "GitHub draft asset upload could not reconcile the asset list." >&2
      return 1
    fi
    matching=()
    if [ -n "$records" ]; then
      while IFS= read -r record; do matching+=("$record"); done <<< "$records"
    fi
    if [ "${#matching[@]}" -gt 1 ]; then
      echo "GitHub draft asset upload found duplicate matching assets." >&2
      return 1
    fi
    if [ "${#matching[@]}" -eq 1 ]; then
      IFS=$'\t' read -r asset_id asset_state <<< "${matching[0]}"
      if [ "$asset_state" = uploaded ]; then
        if gh api -H 'Accept: application/octet-stream' \
          "repos/$GITHUB_REPOSITORY/releases/assets/$asset_id" | cmp -s "$input" -; then
          return 0
        fi
        echo "GitHub draft asset exists with unexpected bytes or is unreadable." >&2
        return 1
      fi
      if [ "$asset_state" != starter ]; then
        echo "GitHub draft asset is in unsupported state $asset_state." >&2
        return 1
      fi
      gh api --method DELETE "repos/$GITHUB_REPOSITORY/releases/assets/$asset_id" >/dev/null
      if ! records="$(ep_draft_asset_records "$release_id" "$asset_name")" || [ -n "$records" ]; then
        echo "Incomplete GitHub draft asset was not authoritatively removed." >&2
        return 1
      fi
    elif [ "$http_status" = 422 ]; then
      echo "GitHub rejected the upload but no matching asset exists." >&2
      return 1
    fi

    if [ "$curl_status" -eq 0 ] && [ "$http_status" != 408 ] && [ "$http_status" != 429 ] && [[ ! "$http_status" =~ ^5[0-9][0-9]$ ]]; then
      echo "GitHub draft asset upload returned unexpected HTTP $http_status without asset evidence." >&2
      return 1
    fi

    if [ "$attempt" -eq "$max_attempts" ]; then
      echo "GitHub draft asset upload remained unavailable after reconciliation." >&2
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
}

ep_publish_draft() {
  local release_id="$1"
  test "$(gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$release_id" -f draft=false --jq .draft)" = false
}
