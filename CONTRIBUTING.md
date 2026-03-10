# Contributing

Thanks for contributing to ContentPipe.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
./start.sh start
```

## Before Opening a PR

Please run:

```bash
python3 -m compileall scripts
pytest
```

## Commit Style

Prefer Conventional Commits:

- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `refactor:` internal restructuring
- `test:` tests
- `chore:` maintenance

## Scope

Small, focused PRs are preferred.
If you touch prompts, config, templates, and code together, explain why in the PR body.
