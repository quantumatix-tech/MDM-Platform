# MySQL Documentation

MySQL-specific documentation for the Migration Platform.

## Documents

| Document | Description |
|---|---|
| [MYSQL_E2E_RUNBOOK.md](MYSQL_E2E_RUNBOOK.md) | Reusable E2E runbook: prepare, run, verify, and troubleshoot MySQL migrations |
| [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md) | MySQL FULL migration flow and direction-specific report evidence |
| [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md) | MySQL object support, evidence status, and validation boundaries |
| [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md) | Local MySQL audit, report-backed results, and provenance caveats |
| [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md) | Local MySQL → Azure MySQL audit and Azure-specific findings |
| [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md) | MySQL implementation limits, evidence boundaries, and environment dependencies |
| [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md) | MySQL E2E setup and structural, data, functional, and negative verification |

## Migration Direction Status

| Direction | Status |
|---|---|
| **Local → Local** | PARTIALLY VALIDATED — MySQL → MySQL reports and local audit evidence exist; endpoint locality is not independently recorded in the later report. See [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md). |
| **Local → Azure** | VALIDATED FOR RECORDED FIXTURES — Successful report-backed runs exist; object/runtime coverage is limited by fixture and Azure configuration. See [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md). |
| **Azure → Local** | VALIDATED FOR RECORDED FIXTURE — Successful report-backed run exists; it is separate from Local → Azure evidence. See [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md). |

## Quick links

- Local → Local config: No dedicated config is checked in; see [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md) before preparing one.
- Local → Azure config: `config/mysql_onpremise_cloud_test.yaml`
- Azure → Local config: `config/mysql_local_test.yaml`
- MySQL connector: `core/connectors/mysql.py`

## Running a local E2E test (Local → Local)

No dedicated Local → Local config is checked in. Do not use `config/mysql_local_test.yaml` unchanged; it configures Azure → Local. Prepare and review a local-only config as described in [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md).

```powershell
# Set secrets for the reviewed Local → Local config
$env:SECRET_mysql_source_pass = "<source_password>"
$env:SECRET_mysql_target_pass = "<target_password>"
```

There is no checked-in Local → Local config, so this README does not provide a runnable migration command for that direction. Once a dedicated config is prepared and reviewed, use the command pattern in [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md) and follow its verification steps.

## Running other E2E directions

- **Local → Azure**: Use `config/mysql_onpremise_cloud_test.yaml` after verifying the local source, Azure target, TLS, and secrets. Results and Azure-specific boundaries are recorded in [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md).
- **Azure → Local**: Use `config/mysql_local_test.yaml` after verifying its Azure source and local target. Direction-specific run evidence and procedures are in [MYSQL_E2E_RUNBOOK.md](MYSQL_E2E_RUNBOOK.md) and [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md).

## References

- Local audit: [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md)
- Local → Azure audit: [MYSQL_LOCAL_TO_AZURE_AUDIT.md](MYSQL_LOCAL_TO_AZURE_AUDIT.md)
- Limitations: [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md)
- Migration flow: [MYSQL_MIGRATION_FLOW.md](MYSQL_MIGRATION_FLOW.md)
- Object support matrix: [MYSQL_OBJECT_SUPPORT_MATRIX.md](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- E2E runbook: [MYSQL_E2E_RUNBOOK.md](MYSQL_E2E_RUNBOOK.md)
- Test guide: [MYSQL_TEST_GUIDE.md](MYSQL_TEST_GUIDE.md)
