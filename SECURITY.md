# Security policy

Please report suspected vulnerabilities with GitHub's private vulnerability
reporting for this repository. Do not open a public issue before a fix is
available.

ToolAhead runs opted-in project commands in disposable workspace copies. Those
copies are isolation from the live checkout, not a hostile-code sandbox: only
allow commands you already trust.
