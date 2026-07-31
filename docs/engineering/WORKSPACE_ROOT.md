# Engineering Workspace Root

The Engineering Platform normally permits Codex to write only inside the
repository that owns the transaction. A maintainer may authorize creation of a
new sibling project under the repository's direct parent directory with this
local, git-ignored configuration:

```json
{
  "workspace": {
    "provisioning_root": "/Users/pcvantol/Documents/GitHub"
  }
}
```

Save it as `.djconnect/engineering-platform.local.json` in the engineering
repository. The configured directory must already exist, must not be a
symlink, and must be the current repository's direct parent. For DJConnect,
that permits new direct sibling projects such as `forge` or `project-x` under
`/Users/pcvantol/Documents/GitHub`; all other paths remain unavailable.

The runner passes this single directory to Codex as an additional writable
directory. This is separate from macOS Full Disk Access and does not grant
unbounded filesystem access.
