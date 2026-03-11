# ContentPipe v0.7.1 — GitHub Release Notes (Draft)

## Summary

ContentPipe v0.7.1 is the first hardening release after the public repository launch.

This release makes the project substantially closer to being:

- deployable by other users
- safe to expose behind a reverse proxy
- maintainable as an open-source repository
- testable in CI

## Highlights

### Access control
A lightweight authentication layer was added.

You can now protect the Web UI and API with:

- `CONTENTPIPE_AUTH_TOKEN`
- browser login page (`/login`)
- API token access via `X-ContentPipe-Token` or Bearer token

### Deployment hardening
This release adds:

- `.env.example`
- `Dockerfile`
- `docker-compose.yml`
- production deployment guidance in README
- reverse proxy / HTTPS example

### CI and tests
A basic GitHub Actions workflow now validates the project with:

- dependency install
- `python -m compileall scripts`
- `pytest -q`

A first test suite was added for:

- auth helpers
- YAML / JSON parsing safety
- core web API behavior

### Logging cleanup
The project replaces remaining `print()`-based operational output with structured `logging`, improving production readability and future observability.

### Safer uploads
Image upload handling now includes lightweight validation without heavily hurting review UX:

- allowed image extensions
- MIME type checks
- size limits
- safer identifier validation

## Included work

- MIT license
- security / contributing docs
- auth middleware and login page
- CI workflow
- tests
- Docker deployment files
- README production deployment section
- logging cleanup across core runtime files

## Upgrade notes

If you deploy this release for other users, you should set at least:

```bash
CONTENTPIPE_AUTH_TOKEN=change-me
CONTENTPIPE_PUBLIC_BASE_URL=https://your-domain.example
OPENCLAW_GATEWAY_URL=http://host.docker.internal:18789
```

If you run in `llm_mode=gateway`, also do this once after deploy/update:

```bash
./start.sh install-agent
openclaw gateway restart
```

This ensures `contentpipe-blank` exists, uses an isolated workspace/agentDir, and can write final artifacts into the official project output directory.

## Suggested GitHub release title

`v0.7.1 — release hardening, auth, CI, and deployment`
