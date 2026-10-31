# AEGIS AI

**Autonomous Enterprise Incident Intelligence Platform**

AEGIS AI is a platform for detecting, investigating, and resolving operational
incidents in enterprise environments. It correlates signals from monitoring
and operational systems, proposes root causes and remediation, and executes
approved actions under human supervision and with a full audit trail.

> **Status:** Foundation stage. The repository currently provides a FastAPI
> backend with service and health endpoints, a pytest suite, and placeholders
> for the frontend and infrastructure. The intelligence features are planned.

## Features

Available now:

- `GET /`: service identity (name, version, status)
- `GET /health`: liveness probe for operators and orchestrators
- Automated tests with pytest

Planned (see [docs/architecture.md](docs/architecture.md)):

- Signal ingestion and incident correlation
- Investigation agents backed by an LLM adapter
- Human approval workflow and audit log
- Controlled remediation execution
- Web dashboard and investigation workspace

## Technology

| Area           | Choice                      |
|----------------|-----------------------------|
| Language       | Python 3.13                 |
| API framework  | FastAPI                     |
| ASGI server    | Uvicorn                     |
| Testing        | pytest, httpx               |
| Planned data   | Oracle Database             |
| Planned UI     | To be decided               |

## Repository layout

```
aegis-ai/
├── backend/          FastAPI application and tests
│   ├── app/          Application package
│   └── tests/        pytest suite
├── frontend/         Placeholder for the user interface
├── infrastructure/   Placeholder for deployment assets
├── docs/             Architecture documentation
├── CLAUDE.md         Conventions and security rules
├── requirements.txt  Pinned Python dependencies
└── pytest.ini        Test configuration
```

## Getting started

These commands target **Windows PowerShell** and **Python 3.13**.

### 1. Create a virtual environment

```powershell
cd C:\Projects\aegis-ai
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy, run once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Run the API

```powershell
python -m uvicorn app.main:app --app-dir backend --reload
```

Then open:

- http://127.0.0.1:8000/ for service information
- http://127.0.0.1:8000/health for the health check
- http://127.0.0.1:8000/docs for interactive API documentation

### 4. Run the tests

```powershell
python -m pytest
```

## Configuration

No runtime configuration is required yet. Future settings will be read from
environment variables. Secrets must never be committed; see the security
rules in [CLAUDE.md](CLAUDE.md).

## Documentation

- [Architecture](docs/architecture.md): target design, components, and phases
- [CLAUDE.md](CLAUDE.md): coding conventions and security rules
- [frontend/README.md](frontend/README.md): frontend placeholder
- [infrastructure/README.md](infrastructure/README.md): infrastructure placeholder

## Contributing

1. Read [CLAUDE.md](CLAUDE.md) before making changes.
2. Keep each change small and covered by tests.
3. Run `python -m pytest` before opening a pull request.
4. Update documentation when behavior or scope changes.

## License

To be determined.
