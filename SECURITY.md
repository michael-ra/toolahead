# Security policy

Please report suspected vulnerabilities with GitHub's private vulnerability
reporting for this repository. Do not open a public issue before a fix is
available.

ToolAhead runs opted-in project commands in disposable workspace copies. Those
copies are isolation from the live checkout, not a hostile-code sandbox: only
allow commands you already trust.

Service commands declared in `toolahead.toml` run unsandboxed in the live
workspace, but never from an untrusted file: they require a one-time
`toolahead trust` of the exact file content. The approval hash is stored
outside the repository with mode 0600 and is revoked automatically by any
change to the file, so a cloned repository cannot execute anything by merely
being opened.
