# Oracle E2E Acceptance Guide

Use only approved isolated Oracle test databases. Setup may modify SOURCE fixtures; FULL modifies TARGET.

## PowerShell E2E Workflow

```powershell
python tests/e2e/oracle/run_setup.py --config config/oracle_e2e_acceptance.yaml
if ($LASTEXITCODE -ne 0) { throw "Oracle E2E setup failed; migration was not started." }
python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full
```

Run this workflow only after environment verification and approval. Do not execute it as part of unit tests.

See 05_Oracle_Testing_and_Acceptance_Runbook.md for local-to-local acceptance.
