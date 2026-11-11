# Workspace Mutation Opt-In

KIVO file *creation* is disabled by default for every configured workspace.
Read-only file intelligence (`list_directory`, `read_text_file`,
`search_filenames`, `get_file_metadata`, `get_disk_usage`) continues to work
with any approved workspace. Mutation requires a separate, explicit
per-workspace opt-in written by the user in `workspaces.json`.

The only mutation operation implemented is the internal, approval-bound
`create_text_file` handler. It is **not registered** in the default tool
registry, so this configuration flag does not by itself expose file creation
to the model.

## Configuration file

Default location:

```
$XDG_CONFIG_HOME/kivo/workspaces.json
```

`XDG_CONFIG_HOME` falls back to `~/.config` when unset. The
`KIVO_WORKSPACES_CONFIG` environment variable may point at a different file
(for example, a test or user-managed configuration).

## Read-only entries (default)

A legacy string entry is always read-only:

```json
{
  "version": 1,
  "workspaces": {
    "my-project": "/home/me/projects/my-project"
  }
}
```

An object entry is also read-only unless `mutation_enabled` is exactly `true`:

```json
{
  "version": 1,
  "workspaces": {
    "my-notes": {
      "root": "/home/me/notes"
    }
  }
}
```

Explicit opt-out is allowed and equivalent to the default:

```json
{
  "version": 1,
  "workspaces": {
    "my-notes": {
      "root": "/home/me/notes",
      "mutation_enabled": false
    }
  }
}
```

## Explicit opt-in

Only this exact form grants the workspace mutation eligibility:

```json
{
  "version": 1,
  "workspaces": {
    "my-notes": {
      "root": "/home/me/notes",
      "mutation_enabled": true
    }
  }
}
```

Rules:

- `root` is required and must be an absolute, existing directory.
- `mutation_enabled` is optional and must be a JSON boolean when present.
  Strings such as `"true"` and numbers such as `1` are rejected.
- Object entries accept only `root` and `mutation_enabled`. Unknown fields are
  rejected, not ignored.
- Legacy string entries remain supported but can never enable mutation.
- Workspace IDs are case-sensitive at lookup time, but IDs that differ only by
  case (for example `demo` and `Demo`) are rejected as ambiguous.
- Duplicate JSON keys anywhere in the file are rejected.

## Failure behavior

The configuration fails closed. Mutation is unavailable when:

- the file is missing (this means "no workspaces", not "allow writes");
- the JSON is malformed, contains duplicate keys, or has unknown fields;
- `version` is not `1`;
- a workspace ID, root path, or object-entry field is invalid;
- the configuration file or its parent directory has unsafe ownership or
  permissions, or is a symlink;
- the configured root is not an absolute existing directory, or is a protected
  system/credential/home location; or
- an optional code-level `mutation_workspace_ids` restriction is active and
  does not include the workspace. The configuration opt-in is mandatory; the
  code-level set can only narrow eligibility further.

The configuration file is not a secret, but its absolute roots are never sent
to the model or returned over IPC. Only workspace identifiers and
canonical relative paths may appear in approval/audit metadata.

## Approval and lifecycle

Eligibility is necessary but not sufficient. Every mutation still requires:

1. a trusted preflight bound to the exact turn ID, tool-call ID, tool name,
   workspace, relative path, content digest, byte count, and filesystem
   identities;
2. an explicit user approval for that exact binding (no approval reuse across
   turns or calls);
3. a successful metadata-only audit intent before the filesystem changes; and
4. atomic no-overwrite installation followed by verification and durability
   reporting.

Approval cards display the operation, workspace ID, relative destination, byte
count, a bounded preview, and a no-overwrite notice. They never include
absolute roots, filesystem identity tokens, approval tokens, or execution
grants.

If a mutation is cancelled or times out after installation, the authoritative
lifecycle record reports the file as committed rather than claiming it was not
created.

## Changing the opt-in

Edit `workspaces.json` directly with a text editor. There is no IPC or
model-facing operation that can change workspace roots or mutation
permissions. The configuration is re-read for every preflight, so an edit
takes effect on the next operation without restarting the backend.
