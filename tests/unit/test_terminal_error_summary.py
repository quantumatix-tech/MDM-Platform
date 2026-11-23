import html

from migration_platform.__main__ import print_data_load_statuses, print_object_failures
from core.reporting.report_builder import ReportBuilder
from core.status_server import _DASHBOARD_HTML


def test_terminal_summary_shows_object_failure_and_redacts_secrets(capsys):
    print_object_failures({
        "phases": {
            "object_failures": [{
                "phase": "create_table",
                "object": "VIRTUAL_COLUMN_VALIDATION",
                "error": "Oracle virtual column TOTAL_AMOUNT has an unsupported expression; password=hunter2",
            }]
        }
    })

    output = capsys.readouterr().out
    assert "Object Errors : 1" in output
    assert "[create_table] VIRTUAL_COLUMN_VALIDATION" in output
    assert "unsupported expression" in output
    assert "hunter2" not in output
    assert "password=<redacted>" in output


def test_terminal_summary_limits_number_of_object_failures(capsys):
    print_object_failures({
        "phases": {
            "object_failures": [
                {"phase": "data_load", "object": f"T{i}", "error": "failed"}
                for i in range(7)
            ]
        }
    })

    output = capsys.readouterr().out
    assert "Object Errors : 7" in output
    assert "2 more; see JSON report" in output
    assert "[data_load] T4" in output
    assert "[data_load] T5" not in output


def test_terminal_summary_falls_back_to_top_level_error(capsys):
    print_object_failures({
        "status": "failed",
        "error": "migration failed: password=hunter2",
    })

    output = capsys.readouterr().out
    assert "[migration] <migration>" in output
    assert "migration failed" in output
    assert "hunter2" not in output


def test_terminal_summary_displays_data_statuses_and_skip_reasons(capsys):
    print_data_load_statuses({
        "phases": {
            "data": {
                "counts": {"SUCCESS": 1, "FAILED": 1, "SKIPPED": 1},
                "tables": {
                    "PARENT": {"status": "FAILED", "reason": "row constraint failed"},
                    "CHILD": {"status": "SKIPPED", "reason": "parent PARENT failed"},
                    "INDEPENDENT": {"status": "SUCCESS", "reason": "loaded"},
                },
            }
        }
    })

    output = capsys.readouterr().out
    assert "SUCCESS 1, FAILED 1, SKIPPED 1" in output
    assert "[FAILED] PARENT: row constraint failed" in output
    assert "[SKIPPED] CHILD: parent PARENT failed" in output
    assert "[SUCCESS] INDEPENDENT: loaded" in output


def test_html_and_json_reports_show_object_failure_and_top_level_errors():
    error = (
        "oracle_schema_replacement_verification failed for object 'TARGET_OWNER': "
        "source objects are missing: CONSTRAINT DMS_CK_F7C9F3680E6B0D614657"
    )
    result = {
        "run_id": "oracle-s4",
        "mode": "full",
        "status": "partial_success",
        "error": error,
        "errors": [error],
        "phases": {
            "object_failures": [{
                "phase": "oracle_schema_replacement_verification",
                "object": "TARGET_OWNER",
                "error": "source objects are missing: CONSTRAINT DMS_CK_F7C9F3680E6B0D614657",
            }],
            "oracle_schema_replacement_verification": {
                "status": "failed", "phase": "oracle_schema_replacement_verification",
                "object": "TARGET_OWNER", "error": error,
            },
        },
    }
    builder = ReportBuilder(result, 1000.0, 1100.0)

    json_report = builder.build_json()
    html_report = builder.build_html()

    assert json_report["status"] == "partial_success"
    assert json_report["error"] == error
    assert json_report["errors"] == [error]
    assert json_report["phases"]["object_failures"][0]["phase"] == (
        "oracle_schema_replacement_verification"
    )
    assert html.escape(error) in html_report
    assert "TARGET_OWNER" in html_report
    assert "DMS_CK_F7C9F3680E6B0D614657" in html_report


def test_html_and_json_reports_include_data_statuses_and_reasons():
    result = {
        "run_id": "dependency-run",
        "mode": "full",
        "status": "partial_success",
        "phases": {
            "discover": {"objects": ["PARENT", "CHILD", "INDEPENDENT"]},
            "data": {
                "status": "PARTIAL_SUCCESS",
                "counts": {"SUCCESS": 1, "FAILED": 1, "SKIPPED": 1},
                "tables": {
                    "PARENT": {"status": "FAILED", "reason": "row constraint failed"},
                    "CHILD": {"status": "SKIPPED", "reason": "FK prerequisite PARENT failed"},
                    "INDEPENDENT": {"status": "SUCCESS", "reason": "loaded"},
                },
            },
            "PARENT": {"status": "FAILED", "failure": 1},
            "CHILD": {"status": "SKIPPED", "failure": 0},
            "INDEPENDENT": {"status": "SUCCESS", "success": 1},
        },
    }
    builder = ReportBuilder(result, 1000.0, 1010.0)

    json_report = builder.build_json()
    html_report = builder.build_html()

    assert json_report["phases"]["data"]["tables"]["CHILD"]["status"] == "SKIPPED"
    assert "Status</th><th>Reason</th>" in html_report
    assert "FAILED" in html_report and "SKIPPED" in html_report and "SUCCESS" in html_report
    assert "FK prerequisite PARENT failed" in html_report


def test_live_dashboard_displays_errors_before_completed_state_and_uses_text_nodes():
    assert "if ((d.errors||[]).length > 0)" in _DASHBOARD_HTML
    assert _DASHBOARD_HTML.index("if ((d.errors||[]).length > 0)") < _DASHBOARD_HTML.index(
        "else if (pct === 100)"
    )
    assert "item.textContent = String(e)" in _DASHBOARD_HTML
