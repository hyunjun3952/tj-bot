# Guidelines

## Structure

`main.py` is the bot entry point: it builds a `discord.py` `commands.Bot` (`TJBot`), registers the cogs, syncs the slash commands, and closes cleanly on SIGTERM. The project is a namespace package run from the repo root (nothing is installed), so there is no `__init__.py` in `tj_bot/`.

Core code lives in `tj_bot/`:

- `client.py` handles TJ Media HTTP access (session cookie refresh, request cap, per-key search locks, result and recent-song caches).
- `parser.py` parses search HTML.
- `translit.py` turns romaji/kana queries into the Hangul spellings TJ indexes (the zero-hit search fallback).
- `models.py` defines Pydantic models and the `SearchType`/`NationType`/`MediaBadge` enums.
- `errors.py` maps exceptions to user-facing messages (`reply_error`, `TJMediaError`).
- `ui.py` contains shared Discord UI helpers: embed-limit utilities and `BasePaginationView`.

Slash commands live under `tj_bot/cogs/`: `__init__.py` holds the `TJCog` base class (shared client and command-error handling, including cooldown messages), `search.py` implements `/검색` and its view, and `recent.py` implements `/최신곡` and its view. Tests in `tests/` are split per module (`test_client.py`, `test_parser.py`, `test_translit.py`, `test_embeds.py`, `test_errors.py`) and use inline HTML fixtures and `httpx.MockTransport`; nothing hits the real TJ site.

## Commands

- `uv sync` installs Python 3.12 runtime and development dependencies from `uv.lock`.
- `uv run python main.py` starts the bot locally.
- `uv run ruff check .` applies the repository's lint rules.
- `uv run ruff format --check .` verifies formatting without rewriting files.
- `uv run mypy .` performs strict type checking.
- `uv run pytest` runs the regression tests in `tests/`.
- `uv run --with pytest-cov pytest --cov=tj_bot --cov-report=term-missing` measures coverage on demand. It is deliberately not a dependency or CI gate: the parser, translit, and models are near 100%, and the uncovered rest is mostly Discord interaction code not worth mocking.
- `uv run pre-commit install` sets up git hooks (ruff, mypy, and basic hygiene checks) to run automatically on `git commit`.

Run all four checks (Ruff lint, Ruff format, mypy, pytest) before opening a pull request; CI runs the same set.

## Style

Use four-space indentation, double quotes, and a 150-character line target as configured in `pyproject.toml`. Use `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_CASE` for constants. Keep imports at module scope and type all production code. Prefer early returns and existing shared helpers such as `BasePaginationView` and embed-limit utilities over duplicated Discord logic. Korean slash-command parameter names are intentional and allowed by Ruff.

Conventions the codebase already follows; match them in new code:

- Favor density — don't split an expression across lines just because it's long.
- Use `X | Y` unions, never `Optional`/`Union`.
- Use the walrus operator (`:=`) where it removes a throwaway line.
- Prefer a comprehension over an accumulator loop when it fits on one line.
- Comments are rare, written in English, and explain *why*, never *what*. User-facing Discord text is Korean in a consistent 해요 tone.
- Use `if TYPE_CHECKING:` for imports that are type-only or would create a cycle.
- Keep Ruff at `select = ["ALL"]` rather than a narrow allowlist; `ASYNC`, `B`, and `S` catch real bugs in an async Discord bot.

## Commits & PRs

History follows Conventional Commit-style subjects such as `fix(search): guard zero-hit reload` and `perf(client): cache search results`. Keep commits focused and use an imperative, scoped subject where useful. Pull requests should explain the behavior change, mention linked issues, list verification commands, and include screenshots for visible Discord embed or interaction changes.

## Security & Configuration

Store `DISCORD_TOKEN` (required) and optional `GUILD_ID` in `.env`; never commit credentials. With `GUILD_ID` set, commands sync to that guild immediately, which suits development; without it they sync globally. Treat TJ Media responses as untrusted external data and validate values before conversion.

## Deployment

Pushes to `master` run CI, and once `check` passes the `deploy` job SSHes into the GCE VM, fetches the tested commit over HTTPS with the run's token, hard-resets the checkout to it, runs `uv sync --frozen --no-dev`, and restarts the `tj-bot` systemd unit. It requires the unit to stay active for 30 seconds and otherwise rolls back to the previous commit. Deploys are serialized, never cancelled. The checkout is reset rather than fast-forwarded so the VM follows any `master` history, including rewrites. Secrets (`DEPLOY_HOST`, `DEPLOY_SSH_KEY`) live in the repository settings.
