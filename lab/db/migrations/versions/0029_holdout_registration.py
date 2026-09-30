"""Use the persisted holdout manifest column in the Director registration probe."""

from alembic import op

revision = "0029_holdout_registration"
down_revision = "0028_mode_snapshot_install"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
DO $repair$
DECLARE body text;
BEGIN
 SELECT pg_get_functiondef('lab.holdout_suite_is_registered(uuid)'::regprocedure) INTO body;
 IF position('version_row.holdout_manifest_sha256' in body)=0 THEN
  RAISE EXCEPTION 'expected holdout registration probe differs';
 END IF;
 body:=replace(body,'version_row.holdout_manifest_sha256','version_row.manifest_sha256');
 EXECUTE body;
END $repair$;
""")


def downgrade() -> None:
    raise RuntimeError("restoring the invalid holdout registry column is unsupported")
