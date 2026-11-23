# Oracle permission migration onboarding

Oracle permission migration is opt-in and uses a dedicated target-side executor.
The configured TARGET schema owner continues to issue only grants on objects it
owns. The executor provisions missing local users in the locked external-account
state, creates non-authenticated roles, applies approved role memberships and
approved `CREATE SESSION` grants, and verifies the resulting metadata. Existing
target principals are reused without changing their password, account state,
profile, or grants.

The source grant inventory is derived from `ALL_TAB_PRIVS_MADE` and
`ALL_COL_PRIVS_MADE`, filtered to the connected source owner. Application
principals are the grantees of those grants plus the connected role-membership
graph. Unsupported grant types, common principals, PUBLIC grants, inherited
grants, and unapproved system privileges fail before FULL replacement cleanup.

## Minimal routine configuration

```yaml
oracle_permission_migration:
  enabled: true
  executor:
    username: ORACLE_PERMISSION_EXECUTOR
    password_secret: oracle_permission_executor_password
  new_user_policy: LOCKED
  approve_role_creation_admin_option: true
  approve_create_session: false
```

The secret is resolved through the configured target secret provider (or the
global provider). Executor host, port, service, and TLS settings are inherited
from TARGET so the connector can verify the executor is in the same database,
PDB, and service as the target owner.

## One-time authorization

The application does not and cannot self-authorize. The source and target
databases may be different instances, so their infrastructure grants are handled
separately.

1. A source DBA grants the connected SOURCE owner read access to the principal
   catalog views required for discovery. `ALL_TAB_PRIVS_MADE` and
   `ALL_COL_PRIVS_MADE` are owner-scoped and are probed separately. The DBA views
   are `DBA_USERS`, `DBA_ROLES`, `DBA_ROLE_PRIVS`, and `DBA_SYS_PRIVS`.
   The exact one-time grants, executed by an authorized DBA in the SOURCE PDB,
   are:

   ```sql
   GRANT SELECT ON SYS.DBA_USERS TO <source_owner>;
   GRANT SELECT ON SYS.DBA_ROLES TO <source_owner>;
   GRANT SELECT ON SYS.DBA_ROLE_PRIVS TO <source_owner>;
   GRANT SELECT ON SYS.DBA_SYS_PRIVS TO <source_owner>;
   ```

   The owner-scoped `ALL_*_PRIVS_MADE` views are also probed; if a deployment
   does not expose them to the owner, the preflight names those exact views.
2. Configure the dedicated target executor and run the FULL preflight once with
   permission migration enabled. It reports all inaccessible views and missing
   executor authority together, before destructive cleanup.
3. If automated onboarding is explicitly approved, provide a separate
   `onboarding.admin_connection` username and secret and enable the narrow
   approvals below. The administrator must already have direct SELECT grant
   option on each required `SYS.DBA_*` view, ADMIN OPTION on each exact system
   privilege being delegated, and ADMIN OPTION on any pre-existing mapped target
   roles receiving new members. Broad `SELECT ANY DICTIONARY`, `DBA`,
   `GRANT ANY ROLE`, and `GRANT ANY PRIVILEGE` are not used. If that direct
   authority is unavailable, a DBA must provision the exact grants once through
   the organization's approved Oracle administration process.
   For catalog visibility, that fallback is one-time `GRANT SELECT ON
   SYS.DBA_<view> TO <executor>` for the six reported views; for user/role/system
   authority, use only the specific privileges reported by preflight. No such
   commands are needed on routine runs after onboarding.
4. Disable onboarding after it succeeds. Routine runs use the already onboarded
   executor and do not issue grants to the executor or require per-principal
   YAML entries.

Example explicit onboarding policy (use only for an approved onboarding run):

```yaml
oracle_permission_migration:
  enabled: true
  executor:
    username: ORACLE_PERMISSION_EXECUTOR
    password_secret: oracle_permission_executor_password
  onboarding:
    enabled: true
    admin_connection:
      username: ORACLE_SECURITY_ONBOARDER
      password_secret: oracle_permission_onboarder_password
    approve_catalog_access: true
    approve_create_user: true
    approve_create_role: true
    approve_create_session: false
    approve_role_admin_option: true
  approve_role_creation_admin_option: true
  approve_create_session: false
```

Onboarding preflights the full operation set before issuing any statement. It
grants SELECT only on the six fixed permission catalog views, plus only the
source-derived `CREATE USER`, `CREATE ROLE`, `CREATE SESSION`, and role ADMIN
OPTION authorities actually needed by that source inventory. It never grants
ANY privileges. New users remain locked and externally authenticated.

## Read-only checks

Run the repository's FULL overwrite-plan command before migration. Permission
preflight then verifies:

- the source owner-scoped object/column grant inventory and connected role graph;
- direct visibility of each required DBA view on source and target executor;
- configured executor identity and target database/PDB/service identity;
- required direct CREATE privileges and ADMIN OPTION for source-derived grants;
- target principal type conflicts, existing role exposure, and existing grant state.

No migration is considered complete if permission application or verification
fails. A denied catalog query is a pre-cleanup error; it is never interpreted as
an empty permission inventory.

## FULL permission reconciliation and provenance

FULL computes the target permission delta from the current source inventory
before schema cleanup. It applies missing supported grants, revokes stale
permissions only when a durable framework provenance row proves that the
framework issued the target permission, and verifies the resulting grants,
memberships, and system privileges. Revoke authority is checked before cleanup;
an executor without the exact role ADMIN OPTION or system privilege ADMIN
OPTION is rejected. Object revokes are restricted to grants made by the target
schema owner on source-managed target objects. Changes are journaled as pending
before Oracle DDL and finalized after catalog confirmation so an interrupted
run can resume without blindly repeating or losing ownership state.

The target schema owner stores the journal in
`DMT_PERMISSION_PROVENANCE`; the dedicated executor receives only SELECT,
INSERT, UPDATE, and DELETE on that framework-owned table. The migration reports
the full preflight grant/revoke delta and any independent target permissions it
preserves. Unrelated target users, roles, grants, and target-only objects are
outside that managed scope.

### Existing permissions from before provenance tracking

An existing target permission absent from SOURCE cannot safely be assumed to
have been created by the migration. Oracle grantor metadata identifies the
granting principal, not the application or process that issued the grant. A
prior success report can help an operator decide, but is not an authoritative
database ownership record. Normal FULL therefore fails closed. For a one-time
interactive review, run the same FULL command with
`--oracle-permission-adopt-legacy`. The CLI displays the exact candidate grants
and requires typing `APPROVE N LEGACY PERMISSIONS`. It then re-runs permission
preflight, confirms the grants still match, verifies revoke authority, writes
their provenance records to TARGET, and continues FULL. Declining the prompt
leaves permissions and schema data unchanged. Future runs use the TARGET ledger
and need no approval flag or permission hashes in YAML.

The provenance marker binds a target owner ledger to a hash of the verified
SOURCE and TARGET Oracle identities (user, DB unique name, PDB, and service).
Reusing one target owner for a different source/target pairing fails closed;
use a separate target owner to keep migration scopes isolated.

## Executor credential workflow

On Windows, the repository Local Store persists encrypted secrets across
PowerShell sessions using Windows Credential Manager for its encryption key.
Store the separate executor password once with
`python -m migration_platform.secrets_cli set oracle_permission_executor_password`.
When the configured global provider is `env`, the executor resolver checks
`SECRET_<executor-secret>` first and falls back to the encrypted Local Store if
the environment reference is absent. SOURCE and TARGET continue to use only
their configured environment secrets.
For deployments where TARGET itself is the explicitly provisioned dedicated
executor, `executor.target_credentials: true` can reuse the TARGET password,
but only when the configured executor username exactly equals the target owner.
This option never elevates that identity; Oracle authority must already have
been granted and is checked in preflight.

`--oracle-overwrite-plan` remains a read-only schema replacement plan; it does
not perform the permission delta inspection. Permission reconciliation is
included in the normal FULL preflight report and completes before cleanup.

## Limits

- Only local application users/roles and SELECT/INSERT/UPDATE/DELETE object
  privileges, INSERT/UPDATE/REFERENCES column privileges, local role
  memberships, and explicitly approved CREATE SESSION are supported.
- Source passwords, password hashes, authentication data, and account state are
  not copied. Newly created users are locked until the target administrator
  provisions their intended authentication separately.
- Existing target roles with unrelated grants cannot safely receive new members;
  preflight fails rather than exposing their target-only access.
- A membership grant to an existing role requires ADMIN OPTION on that specific
  role. The design does not elevate this to GRANT ANY ROLE.
- Source-side DBA-view access requires a one-time DBA authorization. The
  application cannot grant access to its own source catalogs.
