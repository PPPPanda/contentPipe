# Changelog

All notable changes to this project will be documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project loosely follows Semantic Versioning.

## [Unreleased]

### Planned
- More platform templates
- Stronger production deployment defaults
- Richer review/chat editing experience
- More complete publishing integrations

## [0.7.1] - 2026-03-11

### Added
- Web UI / API authentication via `CONTENTPIPE_AUTH_TOKEN`
- Login page for browser-based access control
- API auth via `X-ContentPipe-Token` and Bearer token
- MIT `LICENSE`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `.env.example`
- `Dockerfile`
- `docker-compose.yml`
- GitHub Actions CI workflow
- Test suite for auth, node parsing, and core web API flows
- `requirements.txt` for public repo setup
- Production deployment / reverse proxy / HTTPS guidance in README

### Changed
- Replaced remaining `print()` calls with structured `logging`
- Notification URL generation now supports `CONTENTPIPE_PUBLIC_BASE_URL`
- Discord notification channel is configurable instead of hardcoded in public defaults
- Gateway URL handling is more configurable for public deployment
- Public repository defaults were hardened for external users

### Security
- Added lightweight image upload validation:
  - allowed image extensions whitelist
  - MIME type check
  - 20MB size limit
  - safer `image_name` / `placement_id` validation
- Added access control middleware for non-public routes

### Fixed
- Synced public-facing version references and deployment docs
- Reduced risk of accidentally exposing runtime artifacts in the public repo

## [0.7.0] - 2026-03-10

### Added
- Initial public GitHub repository for ContentPipe plugin
- Managed-service plugin layout under `plugins/content-pipeline/`
- `openclaw.plugin.yaml` manifest
- `start.sh` service manager (`start|stop|restart|status|logs`)
- `/api/health` and `/api/info` endpoints
- Discord notification integration for review / completion / failure events
- Full plugin-oriented `README.md` and `SKILL.md`

### Changed
- Upgraded project shape from skill-oriented packaging to plugin-oriented packaging
- Reorganized the project for public distribution
- Cleaned repository layout for GitHub publishing

### Notes
- This release marks the first public plugin release line.
- It established the deployment surface, web console service model, and external-facing repository structure.
