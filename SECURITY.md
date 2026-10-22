# Security Policy

KNOCK answers the front door. A security bug here can mean a stranger learns
whether anyone is home, or private household data ends up somewhere it
shouldn't -- so reports are taken seriously and handled privately.

## Supported Versions

Only the latest release (and `main`) receives security fixes. Please check
you can still reproduce the issue on the latest version before reporting.

## Reporting a Vulnerability

**Please do not open a public issue, discussion, or pull request for security
problems.**

Report privately through GitHub instead:
[**Report a vulnerability**](https://github.com/jumpinjet22/KNOCK/security/advisories/new)
(Security tab → *Report a vulnerability*).

Helpful things to include:

- what the issue is and what an attacker could do with it
- steps to reproduce, or a proof of concept
- the KNOCK version/commit and how it's deployed (local, Docker, distributed)
- which providers/integrations are involved, if any

Never include real camera footage, recordings, or credentials -- use
redacted or synthetic data.

KNOCK is maintained on a best-effort basis. You can expect an
acknowledgement once the report has been read, updates as it's investigated,
and credit in the advisory once a fix ships (unless you'd rather stay
anonymous). Please give a reasonable window to release a fix before
disclosing publicly.

## Scope

In scope, for example:

- getting KNOCK to reveal occupancy, schedules, or other household
  information to a visitor, or otherwise bypassing the conversation safety
  policy (including via prompt injection)
- authentication/session bypass, CSRF, or privilege issues in the API or web UI
- exposure of secrets (API keys, tokens, passwords) or stored audit/session data
- path traversal, injection, or remote code execution
- integrations (MQTT, Frigate, Home Assistant, UniFi Protect, SIP) being
  abused to trigger unintended actions

Out of scope:

- vulnerabilities in third-party software KNOCK talks to (Ollama, Whisper,
  Frigate, Home Assistant, etc.) -- please report those upstream
- attacks that already require root/admin access to the host running KNOCK
- issues that only occur from deliberately insecure configuration, such as
  exposing the API to the internet without authentication
