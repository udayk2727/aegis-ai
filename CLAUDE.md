# CLAUDE.md

Guidance for AI assistants and human contributors working in this repository.

## Project

AEGIS AI is an Autonomous Enterprise Incident Intelligence Platform. It will
ingest operational signals, correlate incidents, and recommend or execute
remediation with human approval. It is an enterprise system, so correctness,
auditability, and security matter more than speed of delivery.

## Current scope

The repository is at the foundation stage. Do **not** add the following yet
without explicit approval from the project owner:

- Oracle database connections
- LLM or AI provider API integrations
- Docker, Compose, or Kubernetes definitions
- Additional third-party dependencies beyond `requirements.txt` without
  justification in the pull request or commit message

### Day 7 exception: optional local LLM adapter

The project owner has approved one narrow exception to the LLM bullet above. It
covers optional OpenAI Responses API and Anthropic Messages API providers for the
investigation agent, for **local development only**:

- The adapters live in `backend/app/services/`. They are used only by three local
  scripts: `scripts/run_investigation_agent.py`,
  `scripts/anthropic_connectivity_check.py`, and `scripts/evaluate_agent.py`. A
  live provider is selected with `--provider openai` or `--provider anthropic`.
  Every paid execution requires explicit `--live`. There is no automatic live call.
  The fake provider stays the default.
- Dependency exception, narrow: the rule above on additional third-party
  dependencies is relaxed for exactly two packages, the OpenAI Python SDK
  (`openai==2.54.0`) and the Anthropic Python SDK (`anthropic==0.109.0`). Both are
  pinned at exact versions in `requirements-llm.txt`, and installed only for local
  Day 7 development and for the backend job in `.github/workflows/ci.yml`, after
  the core requirements. The CI job installs them so the mocked provider tests run.
  It passes no provider key and makes no live provider call. They are not part of
  `requirements.txt`. No other package is added under this exception. Any other
  new dependency still needs justification in the pull request or commit message,
  and the owner's approval.
- It may use only the SDKs pinned in `requirements-llm.txt`. Nothing else is added.
- Pytest, CI, ordinary imports, and default evaluation runs never call the adapter
  or make a paid request. Tests use mock clients only. A test module for an optional
  provider skips cleanly when that provider's SDK is not installed.
- Credentials come from the environment only: `OPENAI_API_KEY` for the OpenAI
  provider, `ANTHROPIC_API_KEY` for the Anthropic provider, and `AEGIS_LLM_MODEL`,
  which has no default. A key is never hardcoded, committed, logged, or printed.
- The model may request tools. Only the trusted, read-only tool registry runs
  them, and every model output is verified by the server-side verifier before it
  is returned.

This exception does **not** cover production use, any new HTTP endpoint, any
change to authentication or authorization, paid live requests without a separate
owner authorization, autonomous remediation, or any other provider or dependency.
Each of those needs its own explicit approval. Every other rule in this file still
applies in full, including security rules 1 to 10.

## Repository layout

| Path               | Purpose                                              |
|--------------------|------------------------------------------------------|
| `backend/app/`     | FastAPI application package                          |
| `backend/app/routers/` | One module per API feature area                  |
| `backend/tests/`   | pytest test suite                                    |
| `frontend/`        | Placeholder for the user interface                   |
| `infrastructure/`  | Placeholder for deployment assets                    |
| `docs/`            | Architecture and design documents                    |
| `scripts/`         | Offline analysis and approval workflows              |

## Language and runtime

- Target **Python 3.13**. Use modern syntax (`X | None`, built-in generics such
  as `list[str]`, `dict[str, int]`).
- Do not use language features deprecated in 3.13 or newer.

## Coding conventions

- **Style:** PEP 8. Maximum line length 88 characters (Black default).
- **Type hints:** required on all public functions, methods, and module-level
  variables. Return types are always declared.
- **Naming:** `snake_case` for functions, variables, and modules;
  `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants.
- **Docstrings:** every module, public class, and public function has a
  docstring. Describe *what* and *why*, not a line-by-line restatement of the
  code. Use the triple-double-quote style already in the codebase.
- **Modularity:** keep `main.py` as a thin composition root. New features go
  in their own router or service module and are registered in
  `create_app()`.
- **Schemas:** request and response bodies are Pydantic models. Declare
  `response_model` on every route.
- **Imports:** standard library first, then third-party, then local packages,
  with a blank line between groups.
- **No dead code:** do not leave commented-out code or unused imports in the
  repository.
- **Comments:** explain non-obvious intent or constraints. Do not narrate
  obvious code.

## Testing

- Every new endpoint or behavior ships with pytest tests.
- Tests live in `backend/tests/` and use the shared `client` fixture from
  `conftest.py`.
- Run the suite from the repository root with `python -m pytest`.
- Tests must be deterministic: no network calls, no real clock dependence,
  and no reliance on machine-specific paths.

## Security rules

These rules are mandatory. Do not weaken them to make a change easier.

1. **Never commit secrets.** This includes API keys, passwords, tokens,
   private keys, Oracle wallets, and connection strings containing
   credentials. Load configuration from environment variables or a secrets
   manager, never from source files.
2. **Keep `.env` files out of Git.** Only `.env.example` templates with
   placeholder values may be committed.
3. **Do not log sensitive data.** Never log request bodies containing
   credentials, personal data, or full tokens. Log identifiers and outcomes.
4. **Validate all external input.** Every request body, path parameter, and
   query parameter is typed and validated through Pydantic or FastAPI
   declarations.
5. **Expose the minimum.** Do not return stack traces, internal paths,
   dependency versions, or environment details in API responses. The current
   `GET /` returns only the service name, version, and status.
6. **Authenticate before adding write or action endpoints.** No endpoint that
   changes state or triggers remediation may be added until an authentication
   and authorization design is documented in `docs/architecture.md`.
7. **Human approval for remediation.** Autonomous actions against production
   systems require an explicit approval gate. Do not implement automatic
   execution paths that bypass it.
8. **Keep dependencies current and pinned.** Pin exact versions in
   `requirements.txt`. Check for known vulnerabilities before upgrading.
9. **Treat AI model output as untrusted.** When LLM integration is added,
   validate model output before acting on it, and never let it directly
   construct commands, queries, or URLs without sanitization.
10. **Report vulnerabilities privately.** Do not describe an unpatched security
    issue in a public issue, commit message, or pull request.

## Git and change management

- Use short, imperative commit messages (for example, `Add health endpoint`).
- One logical change per commit.
- Do not commit generated, cached, or local environment files; `.gitignore`
  covers the standard cases.
- Do not initialize, push, or publish the repository without owner approval.

## Documentation

- Update `README.md` when setup steps, commands, or scope change.
- Update `docs/architecture.md` when a design decision changes.
- Keep documentation factual. Mark planned items as planned rather than
  describing them as present.
