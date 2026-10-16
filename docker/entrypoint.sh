#!/bin/sh
set -e

# When no arguments are supplied, run a startup smoke test before launching the
# CLI. This verifies that the package imports, the version resolves, and the
# runtime manifest helpers work inside the container, without requiring a
# database connection. When arguments are supplied (e.g. --version, --help, or a
# real migration config), the smoke test is skipped so CLI output stays clean.
if [ $# -eq 0 ]; then
    echo "=== Migration Platform container startup smoke test ==="
    python -c "import migration_platform; print('migration-platform', migration_platform.__version__)"
    python -c "
from core.runtime_manifest import _get_app_version, _get_python_info, _get_package_versions
print('app_version:', _get_app_version())
print('python:', _get_python_info()['version'])
pkgs = _get_package_versions()
print('psycopg:', pkgs.get('psycopg', 'not_installed'))
print('pyodbc:', pkgs.get('pyodbc', 'not_installed'))
print('mysql-connector-python:', pkgs.get('mysql-connector-python', 'not_installed'))
print('pymongo:', pkgs.get('pymongo', 'not_installed'))
"
fi

# Start the Migration Platform CLI, forwarding all arguments.
exec python -m migration_platform "$@"
