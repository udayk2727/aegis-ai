# Frontend

Local, read-only incident dashboard for the Day 9 synthetic demo API. It is
development only and is not authenticated.

**Stack:** React 19, TypeScript, and Vite. Dependencies are pinned exactly in
`package.json` and locked in `package-lock.json`. TypeScript stays on 6.x because
`typescript-eslint` does not yet support 7.x.

**Scope**

- Discovers approved baselines, lists incidents for them, and shows one
  investigation at a time.
- Shows observations, source evidence, ranked hypotheses with their verification
  status, contradictions, missing evidence, and read-only suggested checks.
- Does not start simulations, approve baselines, or change server state.
- Approval workflow, authentication, and any write path are not built.

**Commands** (run from `frontend/`)

| Command         | Purpose                                       |
|-----------------|-----------------------------------------------|
| `npm ci`        | Install the locked dependencies               |
| `npm run dev`   | Start Vite on http://127.0.0.1:5173           |
| `npm test`      | Run the Vitest suite (mocked fetch, no API)   |
| `npm run lint`  | Run ESLint                                    |
| `npm run build` | Typecheck and build to `dist/` (git-ignored)  |

The Vite dev proxy forwards `/api/v1` to `AEGIS_API_TARGET`, which defaults to
`http://127.0.0.1:8001`. Setup, data preparation, and the browser steps are in
the **Dashboard** section of the [demo guide](../docs/demo.md#dashboard).
