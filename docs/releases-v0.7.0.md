# ContentPipe v0.7.0 — GitHub Release Notes (Draft)

## Summary

ContentPipe v0.7.0 is the first public GitHub release of the project as an OpenClaw-managed plugin service.

This release focuses on packaging, service structure, and external-facing repository readiness:

- public plugin repository initialization
- managed-service plugin layout
- health/info endpoints
- service start/stop helpers
- Discord notification integration
- public documentation baseline

## Highlights

### Plugin-oriented packaging
ContentPipe is now organized as a managed-service plugin under `plugins/content-pipeline/`, making the project easier to deploy, document, and publish independently.

### Service management
A dedicated `start.sh` script was added for local lifecycle management:

- `start`
- `stop`
- `restart`
- `status`
- `logs`

### Public repository structure
This release established the first clean public repository structure, including:

- plugin manifest
- Web service entrypoint
- prompts/config/templates layout
- docs structure

### Web service metadata
The following service endpoints are available:

- `/api/health`
- `/api/info`

### Discord notifications
ContentPipe can notify about review waits, completion, and failures through Discord integration.

## Included work

- initial public repository publish
- plugin manifest (`openclaw.plugin.yaml`)
- plugin/skill docs refresh
- baseline public README
- service bootstrap and health endpoints
- notification plumbing

## Upgrade notes

This is the first public release line. If you were previously using an internal workspace copy, review the new plugin directory layout and startup method before migrating.

## Known limitations

- auth and deployment hardening were still minimal in this release
- CI/tests were not yet part of the baseline
- production reverse proxy / HTTPS guidance was still incomplete

## Suggested GitHub release title

`v0.7.0 — first public plugin release`
