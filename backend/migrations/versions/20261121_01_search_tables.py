"""Frozen MySQL 8.0.31 definitions must remain independent of application models."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '20261121_01'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'repositories',
        sa.Column('repository_id', mysql.BIGINT(unsigned=True), autoincrement=False, nullable=False),
        sa.Column('full_name', mysql.VARCHAR(255), nullable=False),
        sa.Column('html_url', mysql.VARCHAR(2048), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('topics', mysql.JSON(none_as_null=True), nullable=True),
        sa.Column('language', mysql.VARCHAR(64), nullable=True),
        sa.Column('stars', mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column('forks', mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column('license', mysql.VARCHAR(128), nullable=True),
        sa.Column('default_branch', mysql.VARCHAR(255), nullable=True),
        sa.Column('pushed_at', mysql.DATETIME(fsp=6), nullable=True),
        sa.Column('is_archived', sa.Boolean(create_constraint=False), nullable=True),
        sa.Column('fetched_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("topics IS NULL OR JSON_TYPE(topics) = 'ARRAY'", name='ck_repo_topics'),
        sa.CheckConstraint('is_archived IS NULL OR is_archived IN (0, 1)', name='ck_repo_archived'),
        sa.CheckConstraint('repository_id > 0 AND CHAR_LENGTH(TRIM(full_name)) > 0 AND CHAR_LENGTH(TRIM(html_url)) > 0', name='ck_repo_identity'),
        sa.PrimaryKeyConstraint('repository_id'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_index('repo_full_name', 'repositories', ['full_name'], unique=False)
    op.create_table(
        'search_plans',
        sa.Column('plan_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('run_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('confirmed_revision_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('planner_version', mysql.VARCHAR(64), nullable=False),
        sa.Column('model_version', mysql.VARCHAR(128), nullable=False),
        sa.Column('prompt_version', mysql.VARCHAR(64), nullable=False),
        sa.Column('plan_storage_key', mysql.VARCHAR(255, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('plan_hash', mysql.CHAR(64, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('plan_status', mysql.VARCHAR(16, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("REGEXP_LIKE(plan_hash, '^[0-9a-f]{64}$', 'c')", name='ck_sp_hash'),
        sa.CheckConstraint("plan_status IN ('pending', 'ready', 'failed')", name='ck_sp_status'),
        sa.CheckConstraint('CHAR_LENGTH(TRIM(plan_storage_key)) > 0', name='ck_sp_storage'),
        sa.CheckConstraint('CHAR_LENGTH(TRIM(planner_version)) > 0 AND CHAR_LENGTH(TRIM(model_version)) > 0 AND CHAR_LENGTH(TRIM(prompt_version)) > 0', name='ck_sp_versions'),
        sa.PrimaryKeyConstraint('plan_id'),
        sa.UniqueConstraint('plan_id', 'run_id', name='sp_plan_run'),
        sa.UniqueConstraint('plan_storage_key', name='sp_storage'),
        sa.UniqueConstraint('run_id', name='sp_run'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_index('sp_revision_created', 'search_plans', ['confirmed_revision_id', 'created_at'], unique=False)
    op.create_table(
        'repository_snapshots',
        sa.Column('snapshot_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('repository_id', mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column('commit_sha', mysql.CHAR(40, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('readme_path', sa.Text(collation='utf8mb4_0900_bin'), nullable=False),
        sa.Column('blob_sha', mysql.CHAR(40, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('content_hash', mysql.CHAR(64, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('storage_key', mysql.VARCHAR(255, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('snapshot_key', mysql.CHAR(64, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('collected_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("REGEXP_LIKE(blob_sha, '^[0-9a-f]{40}$', 'c')", name='ck_rs_blob'),
        sa.CheckConstraint("REGEXP_LIKE(commit_sha, '^[0-9a-f]{40}$', 'c')", name='ck_rs_commit'),
        sa.CheckConstraint("REGEXP_LIKE(content_hash, '^[0-9a-f]{64}$', 'c')", name='ck_rs_content'),
        sa.CheckConstraint("REGEXP_LIKE(snapshot_key, '^[0-9a-f]{64}$', 'c')", name='ck_rs_key'),
        sa.CheckConstraint('CHAR_LENGTH(TRIM(storage_key)) > 0', name='ck_rs_storage'),
        sa.CheckConstraint('CHAR_LENGTH(readme_path) > 0', name='ck_rs_path'),
        sa.ForeignKeyConstraint(['repository_id'], ['repositories.repository_id'], name='fk_rs_repo', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('snapshot_id'),
        sa.UniqueConstraint('snapshot_id', 'repository_id', name='rs_snapshot_repo'),
        sa.UniqueConstraint('snapshot_key', name='rs_identity'),
        sa.UniqueConstraint('storage_key', name='rs_storage'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_index('rs_repo_collected', 'repository_snapshots', ['repository_id', 'collected_at'], unique=False)
    op.create_table(
        'search_groups',
        sa.Column('group_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('plan_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('group_name', mysql.CHAR(1, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('group_purpose', mysql.VARCHAR(255), nullable=False),
        sa.Column('source_requirement_pointers', mysql.JSON(none_as_null=True), nullable=False),
        sa.Column('group_status', mysql.VARCHAR(16, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('skip_reason', mysql.VARCHAR(255), nullable=True),
        sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("(group_status = 'ready' AND skip_reason IS NULL AND JSON_LENGTH(source_requirement_pointers) > 0) OR (group_status = 'skipped' AND skip_reason IS NOT NULL AND CHAR_LENGTH(TRIM(skip_reason)) > 0)", name='ck_sg_skip'),
        sa.CheckConstraint("JSON_TYPE(source_requirement_pointers) = 'ARRAY'", name='ck_sg_pointers'),
        sa.CheckConstraint("group_name IN ('A', 'B', 'C')", name='ck_sg_name'),
        sa.CheckConstraint("group_status IN ('ready', 'skipped')", name='ck_sg_status'),
        sa.CheckConstraint('CHAR_LENGTH(TRIM(group_purpose)) > 0', name='ck_sg_purpose'),
        sa.ForeignKeyConstraint(['plan_id'], ['search_plans.plan_id'], name='fk_sg_plan', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('group_id'),
        sa.UniqueConstraint('plan_id', 'group_name', name='sg_plan_name'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_table(
        'search_queries',
        sa.Column('query_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('group_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('query_text', sa.Text(), nullable=False),
        sa.Column('query_type', mysql.VARCHAR(32, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('language_filter', mysql.VARCHAR(64), nullable=True),
        sa.Column('qualifiers', mysql.JSON(none_as_null=True), nullable=False),
        sa.Column('source_requirement_pointers', mysql.JSON(none_as_null=True), nullable=False),
        sa.Column('query_hash', mysql.CHAR(64, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("JSON_TYPE(qualifiers) = 'OBJECT'", name='ck_sq_qualifiers'),
        sa.CheckConstraint("JSON_TYPE(source_requirement_pointers) = 'ARRAY' AND JSON_LENGTH(source_requirement_pointers) > 0", name='ck_sq_pointers'),
        sa.CheckConstraint("REGEXP_LIKE(query_hash, '^[0-9a-f]{64}$', 'c')", name='ck_sq_hash'),
        sa.CheckConstraint("query_type IN ('primary', 'expansion')", name='ck_sq_type'),
        sa.CheckConstraint('CHAR_LENGTH(TRIM(query_text)) > 0', name='ck_sq_text'),
        sa.CheckConstraint('language_filter IS NULL OR CHAR_LENGTH(TRIM(language_filter)) > 0', name='ck_sq_language'),
        sa.ForeignKeyConstraint(['group_id'], ['search_groups.group_id'], name='fk_sq_group', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('query_id'),
        sa.UniqueConstraint('group_id', 'query_hash', name='sq_group_hash'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_table(
        'search_snapshots',
        sa.Column('id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('run_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('plan_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('repository_id', mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column('snapshot_id', mysql.CHAR(36, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('usage_type', mysql.VARCHAR(32, charset='ascii', collation='ascii_bin'), nullable=False),
        sa.Column('used_at', mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("usage_type IN ('candidate_review', 'evidence_retrieval', 'deep_analysis')", name='ck_ss_usage'),
        sa.ForeignKeyConstraint(['plan_id', 'run_id'], ['search_plans.plan_id', 'search_plans.run_id'], name='fk_ss_plan_run', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['snapshot_id', 'repository_id'], ['repository_snapshots.snapshot_id', 'repository_snapshots.repository_id'], name='fk_ss_snapshot_repo', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'repository_id', 'snapshot_id', 'usage_type', name='ss_run_repo_snapshot_usage'),
        mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci', mysql_engine='InnoDB',
    )
    op.create_index('ss_plan_run', 'search_snapshots', ['plan_id', 'run_id'], unique=False)
    op.create_index('ss_snapshot_repo', 'search_snapshots', ['snapshot_id', 'repository_id'], unique=False)


def downgrade():
    op.drop_table('search_snapshots')
    op.drop_table('search_queries')
    op.drop_table('search_groups')
    op.drop_table('repository_snapshots')
    op.drop_table('search_plans')
    op.drop_table('repositories')
