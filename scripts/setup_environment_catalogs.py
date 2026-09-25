"""Create the staging and prod Unity Catalog catalogs for the CI/CD targets (idempotent).

Each catalog gets managed storage on the project's ADLS account under the existing external location
(abfss://metastore@stsent7s5fwynthfd64.dfs.core.windows.net/<env>), is bound to this workspace only,
has predictive optimization disabled (no background compute), and grants ALL PRIVILEGES to its own
CI service principal, which deploys and runs that target. The human owner keeps ownership.

  . ./scripts/Use-SentinelOps.ps1
  .venv/Scripts/python.exe scripts/setup_environment_catalogs.py
"""
import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service import catalog as uc

WORKSPACE_ID = 7405619144539463
STORAGE = "abfss://metastore@stsent7s5fwynthfd64.dfs.core.windows.net"
ENVIRONMENTS = {
    "sentinelops_staging": ("staging", "02bece01-ccd7-4130-8e8d-c8de43583e41"),  # sentinelops-staging-ci
    "sentinelops_prod": ("prod", "5d729946-2748-48f4-9300-ba9a559bf93d"),  # sentinelops-prod-ci
}

client = WorkspaceClient()
for name, (path, principal) in ENVIRONMENTS.items():
    try:
        client.catalogs.get(name)
    except NotFound:
        client.catalogs.create(name, storage_root=f"{STORAGE}/{path}",
                               comment=f"SentinelOps {path}: deployed and run by its CI service principal")
    except PermissionDenied:
        pass  # It exists but is isolated without a binding to this workspace; the binding below fixes that.
    # Bind before isolating: an isolated catalog with no binding is unreachable from every workspace.
    client.workspace_bindings.update_bindings(
        "catalog", name,
        add=[uc.WorkspaceBinding(workspace_id=WORKSPACE_ID, binding_type=uc.WorkspaceBindingBindingType.BINDING_TYPE_READ_WRITE)])
    client.catalogs.update(name, isolation_mode=uc.CatalogIsolationMode.ISOLATED,
                           enable_predictive_optimization=uc.EnablePredictiveOptimization.DISABLE)
    client.grants.update("catalog", name,
                         changes=[uc.PermissionsChange(principal=principal, add=[uc.Privilege.ALL_PRIVILEGES])])
    info = client.catalogs.get(name)
    grants = client.grants.get("catalog", name)
    print(json.dumps({"catalog": name, "owner": info.owner, "storage_root": info.storage_root,
                      "isolation_mode": str(info.isolation_mode), "predictive_optimization":
                          str(info.enable_predictive_optimization),
                      "grants": {a.principal: sorted(str(p) for p in a.privileges or [])
                                 for a in grants.privilege_assignments or []}}))
