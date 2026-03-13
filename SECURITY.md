# SECURITY.md

## Security Policy for OpenClaw Operations

**Purpose:** To outline security best practices and invariants for managing the OpenClaw agent and its associated infrastructure.

## Core Principles

1.  **Least Privilege:** All operations and access should be granted only with the minimum necessary permissions.
2.  **Secrets Management:** Sensitive information (API keys, tokens, passwords) must NEVER be committed to version control. Use secure methods for storage and access.
3.  **Code Integrity:** All scripts and configurations must be reviewed for security vulnerabilities before deployment.
4.  **Auditing and Monitoring:** Regular audits of access logs, system configurations, and script behaviors are essential.
5.  **Dependency Management:** Keep all libraries and tools updated to mitigate known vulnerabilities.

## Handling of Sensitive Information

-   **DO NOT commit:** Configuration files containing secrets (`.env`, `openclaw.json` (sensitive parts), auth-profile files, tokens, keys, certificates) to any repository, public or private.
-   **USE:** macOS Keychain for storing sensitive credentials where possible.
-   **USE:** Environment variables sourced securely (e.g., from `.env` files with strict permissions, or managed by secure system services like `launchd`) for runtime configurations.
-   **NEVER:** Embed secrets directly in scripts or hardcode them in any persistent file that might be committed.

## Repository Security

-   **Private Repositories:** All operational repositories must be private.
-   **Branching Strategy:** Use a controlled branching strategy for changes, requiring review before merging into the main branch.
-   **Secret Scanning:** Enable GitHub's built-in secret scanning for repositories to detect accidental commits of sensitive data.

## Incident Response

-   In case of a suspected security incident, immediately revoke compromised credentials and initiate the recovery procedure. Document the incident and lessons learned in the relevant post-mortem notes.
