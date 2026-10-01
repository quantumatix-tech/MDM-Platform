from importlib.metadata import version

from core.connectors import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Column,
    Schema,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
)
from core.orchestrator import MigrationOrchestrator
from core.validator import Validator
from core.audit_logger import audit_log, get_run_id, set_run_id
from core.retry import retry_with_backoff, CircuitBreaker
from core.secrets import SecretProvider, SecretResolver, EnvSecretProvider, AzureKeyVaultProvider
from core.schema_mapping import TypeMappingRegistry, TypeMapper
from core.assessment import AssessmentReport, AssessmentReportGenerator
from core.alerting import (
    Notifier,
    WebhookNotifier,
    SlackNotifier,
    TeamsNotifier,
    EmailNotifier,
    create_notifier,
)
from core.status_server import StatusServer

__version__ = version("migration-platform")

__all__ = [
    "__version__",
    "SourceConnector",
    "TargetConnector",
    "CDCEngine",
    "Column",
    "Schema",
    "UpsertResult",
    "ApplyResult",
    "ChangeEvent",
    "validate_identifier",
    "MigrationOrchestrator",
    "Validator",
    "audit_log",
    "get_run_id",
    "set_run_id",
    "retry_with_backoff",
    "CircuitBreaker",
    "SecretProvider",
    "SecretResolver",
    "EnvSecretProvider",
    "AzureKeyVaultProvider",
    "TypeMappingRegistry",
    "TypeMapper",
    "AssessmentReport",
    "AssessmentReportGenerator",
    "Notifier",
    "WebhookNotifier",
    "SlackNotifier",
    "TeamsNotifier",
    "EmailNotifier",
    "create_notifier",
    "StatusServer",
]