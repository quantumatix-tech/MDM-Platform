# Oracle LIVE E2E onboarding

The Oracle LIVE E2E launcher uses the existing Oracle E2E runner. Normal FULL
migration remains separate from fixture setup and onboarding.

1. Create the SOURCE and TARGET PDBs and provide PDB-local provisioning admins
   through the configured secret provider.
2. Copy the `e2e.onboarding_profile` block from
   `config/e2e_onboarding_profile.example.yaml` into the acceptance YAML.
   Set approved tablespaces, finite quotas, storage mode, and (only when using
   explicit storage creation) the approved size/path. Keep passwords in the
   secret provider. Existing endpoint/resource/PDB mappings remain the source
   of owner names and PDB identities.
3. Set the profile `enabled` to `true` after values have DBA approval. The
   launcher derives the exact fixture owner privileges from configured SQL.
   Set `expected_db_unique_name` to the verified local CDB identity. If the
   current process does not already set `ORACLE_SID`, supply the verified
   local instance SID as `local_oracle_sid`. The launcher sets it only in its
   SQL*Plus child process. `sqlplus_executable` is optional; otherwise SQL*Plus
   is selected from `PATH` and its matching Oracle Home is derived for that
   child only.
4. From an interactive PowerShell terminal, run:

   ```powershell
   .\scripts\oracle_live_e2e.ps1 -Config config/oracle_e2e_acceptance.yaml
   ```

The launcher first performs read-only SQL*Plus discovery and validates admin
logins. It prints a deterministic plan covering the two PDBs, missing exact
admin grants, configured storage, owner provisioning, permission-executor
setup, fixture files, FULL migration, and validation. It asks for one
`APPLY-<plan hash>` approval. Immediately before calling the existing runner it
repeats discovery; any plan change stops without DDL and requires a new review.

For a read-only pass that prints the verified plan without approval or setup,
use `-PreflightOnly`. Local SYSDBA discovery uses `local_oracle_sid` for the
SQL*Plus child process, or requires `ORACLE_SID` to already exist in the
launching process; it never defaults to a guessed SID. On Windows, when the
selected SQL*Plus belongs to an identifiable local Oracle Home, discovery also
checks the corresponding `OracleService<SID>` state.

After approval, the existing `run_e2e.py --apply --dba-bootstrap` performs
approved missing-resource setup in dependency order. Existing accounts are
never password-reset, nonempty fixture owners stop the run, and the TARGET is
never cleaned or reset. A partial prior bootstrap is inventoried and only
still-missing resources are planned. If an existing owner's tablespace or
quota differs from the profile, the launcher stops rather than altering it.
