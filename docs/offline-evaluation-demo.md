# Offline agent evaluation walkthrough

From the repository root, run the offline evaluator:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_agent.py
```

The evaluator uses a scripted fake provider. The Day 8 baseline recorded
24 verified completions out of 24 completion cases and handled all 216
expected failure cases. All 168 deliberately faulty answers were rejected.

To inspect the ordered lifecycle events, run:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --json --trace
```

In the simulated timeout case, `list_observations` succeeds, the next provider
turn times out, and the run ends with `provider_error`. The result retains
one successful tool call and contains zero accepted claims.