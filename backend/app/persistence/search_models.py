from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKeyConstraint, Index, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, JSON, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SearchBase(DeclarativeBase):
    pass


def ascii_char(length):
    return CHAR(length, charset='ascii', collation='ascii_bin')


def ascii_varchar(length):
    return VARCHAR(length, charset='ascii', collation='ascii_bin')


TABLE_OPTIONS = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}


class SearchPlan(SearchBase):
    __tablename__ = 'search_plans'
    __table_args__ = (
        UniqueConstraint('run_id', name='sp_run'),
        UniqueConstraint('plan_id', 'run_id', name='sp_plan_run'),
        UniqueConstraint('plan_storage_key', name='sp_storage'),
        Index('sp_revision_created', 'confirmed_revision_id', 'created_at'),
        CheckConstraint("plan_status IN ('pending', 'ready', 'failed')", name='ck_sp_status'),
        CheckConstraint('CHAR_LENGTH(TRIM(planner_version)) > 0 AND CHAR_LENGTH(TRIM(model_version)) > 0 AND CHAR_LENGTH(TRIM(prompt_version)) > 0', name='ck_sp_versions'),
        CheckConstraint('CHAR_LENGTH(TRIM(plan_storage_key)) > 0', name='ck_sp_storage'),
        CheckConstraint("REGEXP_LIKE(plan_hash, '^[0-9a-f]{64}$', 'c')", name='ck_sp_hash'),
        TABLE_OPTIONS,
    )

    plan_id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ascii_char(36))
    confirmed_revision_id: Mapped[str] = mapped_column(ascii_char(36))
    planner_version: Mapped[str] = mapped_column(VARCHAR(64))
    model_version: Mapped[str] = mapped_column(VARCHAR(128))
    prompt_version: Mapped[str] = mapped_column(VARCHAR(64))
    plan_storage_key: Mapped[str] = mapped_column(ascii_varchar(255))
    plan_hash: Mapped[str] = mapped_column(ascii_char(64))
    plan_status: Mapped[str] = mapped_column(ascii_varchar(16))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))


class SearchGroup(SearchBase):
    __tablename__ = 'search_groups'
    __table_args__ = (
        UniqueConstraint('plan_id', 'group_name', name='sg_plan_name'),
        ForeignKeyConstraint(['plan_id'], ['search_plans.plan_id'], name='fk_sg_plan', ondelete='RESTRICT', onupdate='RESTRICT'),
        CheckConstraint("group_name IN ('A', 'B', 'C')", name='ck_sg_name'),
        CheckConstraint("group_status IN ('ready', 'skipped')", name='ck_sg_status'),
        CheckConstraint('CHAR_LENGTH(TRIM(group_purpose)) > 0', name='ck_sg_purpose'),
        CheckConstraint("JSON_TYPE(source_requirement_pointers) = 'ARRAY'", name='ck_sg_pointers'),
        CheckConstraint("(group_status = 'ready' AND skip_reason IS NULL AND JSON_LENGTH(source_requirement_pointers) > 0) OR (group_status = 'skipped' AND skip_reason IS NOT NULL AND CHAR_LENGTH(TRIM(skip_reason)) > 0)", name='ck_sg_skip'),
        TABLE_OPTIONS,
    )

    group_id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ascii_char(36))
    group_name: Mapped[str] = mapped_column(ascii_char(1))
    group_purpose: Mapped[str] = mapped_column(VARCHAR(255))
    source_requirement_pointers: Mapped[list[str]] = mapped_column(JSON(none_as_null=True))
    group_status: Mapped[str] = mapped_column(ascii_varchar(16))
    skip_reason: Mapped[str | None] = mapped_column(VARCHAR(255))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))


class SearchQuery(SearchBase):
    __tablename__ = 'search_queries'
    __table_args__ = (
        UniqueConstraint('group_id', 'query_hash', name='sq_group_hash'),
        ForeignKeyConstraint(['group_id'], ['search_groups.group_id'], name='fk_sq_group', ondelete='RESTRICT', onupdate='RESTRICT'),
        CheckConstraint("query_type IN ('primary', 'expansion')", name='ck_sq_type'),
        CheckConstraint('CHAR_LENGTH(TRIM(query_text)) > 0', name='ck_sq_text'),
        CheckConstraint('language_filter IS NULL OR CHAR_LENGTH(TRIM(language_filter)) > 0', name='ck_sq_language'),
        CheckConstraint("JSON_TYPE(qualifiers) = 'OBJECT'", name='ck_sq_qualifiers'),
        CheckConstraint("JSON_TYPE(source_requirement_pointers) = 'ARRAY' AND JSON_LENGTH(source_requirement_pointers) > 0", name='ck_sq_pointers'),
        CheckConstraint("REGEXP_LIKE(query_hash, '^[0-9a-f]{64}$', 'c')", name='ck_sq_hash'),
        TABLE_OPTIONS,
    )

    query_id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(ascii_char(36))
    query_text: Mapped[str] = mapped_column(Text)
    query_type: Mapped[str] = mapped_column(ascii_varchar(32))
    language_filter: Mapped[str | None] = mapped_column(VARCHAR(64))
    qualifiers: Mapped[dict[str, list[str]]] = mapped_column(JSON(none_as_null=True))
    source_requirement_pointers: Mapped[list[str]] = mapped_column(JSON(none_as_null=True))
    query_hash: Mapped[str] = mapped_column(ascii_char(64))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))


class Repository(SearchBase):
    __tablename__ = 'repositories'
    __table_args__ = (
        Index('repo_full_name', 'full_name'),
        CheckConstraint('repository_id > 0 AND CHAR_LENGTH(TRIM(full_name)) > 0 AND CHAR_LENGTH(TRIM(html_url)) > 0', name='ck_repo_identity'),
        CheckConstraint("topics IS NULL OR JSON_TYPE(topics) = 'ARRAY'", name='ck_repo_topics'),
        CheckConstraint('is_archived IS NULL OR is_archived IN (0, 1)', name='ck_repo_archived'),
        TABLE_OPTIONS,
    )

    repository_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(VARCHAR(255))
    html_url: Mapped[str] = mapped_column(VARCHAR(2048))
    description: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    language: Mapped[str | None] = mapped_column(VARCHAR(64))
    stars: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    forks: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    license: Mapped[str | None] = mapped_column(VARCHAR(128))
    default_branch: Mapped[str | None] = mapped_column(VARCHAR(255))
    pushed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    is_archived: Mapped[bool | None] = mapped_column(Boolean(create_constraint=False))
    fetched_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))


class RepositorySnapshot(SearchBase):
    __tablename__ = 'repository_snapshots'
    __table_args__ = (
        UniqueConstraint('snapshot_key', name='rs_identity'),
        UniqueConstraint('storage_key', name='rs_storage'),
        UniqueConstraint('snapshot_id', 'repository_id', name='rs_snapshot_repo'),
        Index('rs_repo_collected', 'repository_id', 'collected_at'),
        ForeignKeyConstraint(['repository_id'], ['repositories.repository_id'], name='fk_rs_repo', ondelete='RESTRICT', onupdate='RESTRICT'),
        CheckConstraint('CHAR_LENGTH(readme_path) > 0', name='ck_rs_path'),
        CheckConstraint('CHAR_LENGTH(TRIM(storage_key)) > 0', name='ck_rs_storage'),
        CheckConstraint("REGEXP_LIKE(commit_sha, '^[0-9a-f]{40}$', 'c')", name='ck_rs_commit'),
        CheckConstraint("REGEXP_LIKE(blob_sha, '^[0-9a-f]{40}$', 'c')", name='ck_rs_blob'),
        CheckConstraint("REGEXP_LIKE(content_hash, '^[0-9a-f]{64}$', 'c')", name='ck_rs_content'),
        CheckConstraint("REGEXP_LIKE(snapshot_key, '^[0-9a-f]{64}$', 'c')", name='ck_rs_key'),
        TABLE_OPTIONS,
    )

    snapshot_id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True)
    repository_id: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    commit_sha: Mapped[str] = mapped_column(ascii_char(40))
    readme_path: Mapped[str] = mapped_column(Text(collation='utf8mb4_0900_bin'))
    blob_sha: Mapped[str] = mapped_column(ascii_char(40))
    content_hash: Mapped[str] = mapped_column(ascii_char(64))
    storage_key: Mapped[str] = mapped_column(ascii_varchar(255))
    snapshot_key: Mapped[str] = mapped_column(ascii_char(64))
    collected_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))


class SearchSnapshot(SearchBase):
    __tablename__ = 'search_snapshots'
    __table_args__ = (
        UniqueConstraint('run_id', 'repository_id', 'snapshot_id', 'usage_type', name='ss_run_repo_snapshot_usage'),
        ForeignKeyConstraint(['plan_id', 'run_id'], ['search_plans.plan_id', 'search_plans.run_id'], name='fk_ss_plan_run', ondelete='RESTRICT', onupdate='RESTRICT'),
        ForeignKeyConstraint(['snapshot_id', 'repository_id'], ['repository_snapshots.snapshot_id', 'repository_snapshots.repository_id'], name='fk_ss_snapshot_repo', ondelete='RESTRICT', onupdate='RESTRICT'),
        Index('ss_plan_run', 'plan_id', 'run_id'),
        Index('ss_snapshot_repo', 'snapshot_id', 'repository_id'),
        CheckConstraint("usage_type IN ('candidate_review', 'evidence_retrieval', 'deep_analysis')", name='ck_ss_usage'),
        TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ascii_char(36))
    plan_id: Mapped[str] = mapped_column(ascii_char(36))
    repository_id: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    snapshot_id: Mapped[str] = mapped_column(ascii_char(36))
    usage_type: Mapped[str] = mapped_column(ascii_varchar(32))
    used_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
