# secrets.policy.md

## Secrets Policy for OpenClaw Operations

**Purpose:** Define the policy for managing and handling secrets within the OpenClaw operational environment.

## Policy Directives

### 1. Secrets are Never Committed

*   **Strict Prohibition:** Secrets (API keys, tokens, passwords, certificates, etc.) must NEVER be committed to any version control repository, including local `.gitignore`d files or private repositories.
*   **Rationale:** This prevents accidental exposure of sensitive credentials, which could lead to unauthorized access or compromise of services.

### 2. Secure Storage Mechanisms

*   **macOS Keychain:** Utilize the macOS Keychain as the primary secure storage for sensitive credentials whenever possible. Tools like `security` CLI or APIs should be used to interact with it.
*   **Environment Variables (Securely Sourced):** For secrets that must be available at runtime (e.g., for scripts executed by `launchd`), use environment variables. These should be sourced from secure, permission-controlled files (e.g., `~/.openclaw/env/*.env` with `chmod 600`) or injected directly by system services like `launchd` (via `.plist` files).
*   **DO NOT USE:** Plaintext files in the repository, unencrypted local storage, or hardcoding within scripts.

### 3. Access Control

*   **File Permissions:** Any files that temporarily store secrets (e.g., `.env` files) must have strict read permissions (e.g., `chmod 600`).
*   **Access Scrutiny:** Access to systems and files containing secrets should be audited and restricted.

### 4. Token Rotation

*   Implement a process for regular rotation of sensitive tokens and keys.
*   Ensure that rotation procedures include updating all relevant locations: Trilium notes, configuration files, environment variables, and any other system that consumes these secrets.

### 5. Incident Response for Secrets

*   If a secret is suspected of compromise, immediately revoke it and rotate it across all affected systems. Document the incident and remediation steps.

--- End of Policy ---