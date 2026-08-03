# Engineering Workspace Authorization

The Engineering Platform normally permits Codex to write only inside the
repository that owns the transaction. Genesis targets require trusted local
host authorization as well as Workspace Preflight. Authorization never skips
Git, worktree or lifecycle checks.

Use the versioned authorization section in the git-ignored local host
configuration, `.engineering/engineering-platform.local.json`:

```json
{
  "workspace": {
    "workspace_authorization": {
      "allowed_roots": [
        {
          "path": "/Users/pcvantol/Documents/GitHub",
          "repository_scope": "direct_children"
        }
      ],
      "allowed_repositories": [],
      "denied_repositories": [],
      "symlink_policy": "reject",
      "case_sensitivity": "host"
    }
  }
}
```

Configured roots and repositories must be absolute, existing paths. Roots
cannot be filesystem roots or symlinks. `direct_children` permits only
immediate repository children, so the example explicitly permits both
`/Users/pcvantol/Documents/GitHub/djconnect` and
`/Users/pcvantol/Documents/GitHub/forge`, but not
`/Users/pcvantol/Documents/GitHub/group/forge`. Use `descendants` only when
nested repositories are intended.

An explicit `allowed_repositories` entry can authorize one repository outside
the roots. `denied_repositories` always wins. Canonical, path-aware containment
rejects traversal, prefix lookalikes and symlink escapes; it never discovers or
authorizes repositories automatically. `symlink_policy` is either `reject` or
`canonicalize_within_root`; the former is the default. `case_sensitivity` is
explicitly recorded as `host` or `sensitive` for host-appropriate policy
review.

Legacy `provisioning_root` remains supported with its original direct-child
semantics. It is intentionally not expanded into authorization for sibling
repositories: migrate by adding the section above. A missing or invalid new
authorization section fails closed for Genesis.

The runner passes only trusted configured roots (or the parent of an explicit
repository) as additional writable directories. This is separate from macOS
Full Disk Access and does not grant unrestricted arbitrary-path execution.
