# Security Policy

## Supported Versions

Current maintained line:

- `0.7.x`

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security-sensitive findings.

Instead, report privately to the maintainer with:

- affected version / commit
- reproduction steps
- expected vs actual behavior
- impact assessment
- suggested fix (optional)

## Deployment Guidance

Before exposing ContentPipe beyond localhost, you should:

- set `CONTENTPIPE_AUTH_TOKEN`
- place the service behind HTTPS / reverse proxy
- restrict network exposure where possible
- keep API keys in environment variables, not committed files
- review upload limits and publishing credentials
