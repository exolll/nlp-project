-- MySQL 8.0.31 / InnoDB. Review only; not executed by the application.
-- Choose manual SQL OR Alembic upgrade, not both on the same empty database.
USE `projectpilot_search`;

CREATE TABLE search_plans (
    plan_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    confirmed_revision_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    planner_version VARCHAR(64) NOT NULL,
    model_version VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    plan_storage_key VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plan_status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (plan_id),
    CONSTRAINT sp_run UNIQUE (run_id),
    CONSTRAINT sp_plan_run UNIQUE (plan_id, run_id),
    CONSTRAINT sp_storage UNIQUE (plan_storage_key),
    INDEX sp_revision_created (confirmed_revision_id, created_at),
    CONSTRAINT ck_sp_status CHECK (plan_status IN ('pending', 'ready', 'failed')),
    CONSTRAINT ck_sp_versions CHECK (CHAR_LENGTH(TRIM(planner_version)) > 0 AND CHAR_LENGTH(TRIM(model_version)) > 0 AND CHAR_LENGTH(TRIM(prompt_version)) > 0),
    CONSTRAINT ck_sp_storage CHECK (CHAR_LENGTH(TRIM(plan_storage_key)) > 0),
    CONSTRAINT ck_sp_hash CHECK (REGEXP_LIKE(plan_hash, '^[0-9a-f]{64}$', 'c'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE search_groups (
    group_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plan_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    group_name CHAR(1) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    group_purpose VARCHAR(255) NOT NULL,
    source_requirement_pointers JSON NOT NULL,
    group_status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    skip_reason VARCHAR(255) NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (group_id),
    CONSTRAINT sg_plan_name UNIQUE (plan_id, group_name),
    CONSTRAINT fk_sg_plan FOREIGN KEY (plan_id) REFERENCES search_plans (plan_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_sg_name CHECK (group_name IN ('A', 'B', 'C')),
    CONSTRAINT ck_sg_status CHECK (group_status IN ('ready', 'skipped')),
    CONSTRAINT ck_sg_purpose CHECK (CHAR_LENGTH(TRIM(group_purpose)) > 0),
    CONSTRAINT ck_sg_pointers CHECK (JSON_TYPE(source_requirement_pointers) = 'ARRAY'),
    CONSTRAINT ck_sg_skip CHECK ((group_status = 'ready' AND skip_reason IS NULL AND JSON_LENGTH(source_requirement_pointers) > 0) OR (group_status = 'skipped' AND skip_reason IS NOT NULL AND CHAR_LENGTH(TRIM(skip_reason)) > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE search_queries (
    query_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    group_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    query_text TEXT NOT NULL,
    query_type VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    language_filter VARCHAR(64) NULL,
    qualifiers JSON NOT NULL,
    source_requirement_pointers JSON NOT NULL,
    query_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (query_id),
    CONSTRAINT sq_group_hash UNIQUE (group_id, query_hash),
    CONSTRAINT fk_sq_group FOREIGN KEY (group_id) REFERENCES search_groups (group_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_sq_type CHECK (query_type IN ('primary', 'expansion')),
    CONSTRAINT ck_sq_text CHECK (CHAR_LENGTH(TRIM(query_text)) > 0),
    CONSTRAINT ck_sq_language CHECK (language_filter IS NULL OR CHAR_LENGTH(TRIM(language_filter)) > 0),
    CONSTRAINT ck_sq_qualifiers CHECK (JSON_TYPE(qualifiers) = 'OBJECT'),
    CONSTRAINT ck_sq_pointers CHECK (JSON_TYPE(source_requirement_pointers) = 'ARRAY' AND JSON_LENGTH(source_requirement_pointers) > 0),
    CONSTRAINT ck_sq_hash CHECK (REGEXP_LIKE(query_hash, '^[0-9a-f]{64}$', 'c'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repositories (
    repository_id BIGINT UNSIGNED NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    html_url VARCHAR(2048) NOT NULL,
    description TEXT NULL,
    topics JSON NULL,
    language VARCHAR(64) NULL,
    stars BIGINT UNSIGNED NULL,
    forks BIGINT UNSIGNED NULL,
    license VARCHAR(128) NULL,
    default_branch VARCHAR(255) NULL,
    pushed_at DATETIME(6) NULL,
    is_archived BOOL NULL,
    fetched_at DATETIME(6) NOT NULL,
    PRIMARY KEY (repository_id),
    INDEX repo_full_name (full_name),
    CONSTRAINT ck_repo_identity CHECK (repository_id > 0 AND CHAR_LENGTH(TRIM(full_name)) > 0 AND CHAR_LENGTH(TRIM(html_url)) > 0),
    CONSTRAINT ck_repo_topics CHECK (topics IS NULL OR JSON_TYPE(topics) = 'ARRAY'),
    CONSTRAINT ck_repo_archived CHECK (is_archived IS NULL OR is_archived IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repository_snapshots (
    snapshot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    repository_id BIGINT UNSIGNED NOT NULL,
    commit_sha CHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    readme_path TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
    blob_sha CHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    storage_key VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    snapshot_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    collected_at DATETIME(6) NOT NULL,
    PRIMARY KEY (snapshot_id),
    CONSTRAINT rs_identity UNIQUE (snapshot_key),
    CONSTRAINT rs_storage UNIQUE (storage_key),
    CONSTRAINT rs_snapshot_repo UNIQUE (snapshot_id, repository_id),
    INDEX rs_repo_collected (repository_id, collected_at),
    CONSTRAINT fk_rs_repo FOREIGN KEY (repository_id) REFERENCES repositories (repository_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_rs_path CHECK (CHAR_LENGTH(readme_path) > 0),
    CONSTRAINT ck_rs_storage CHECK (CHAR_LENGTH(TRIM(storage_key)) > 0),
    CONSTRAINT ck_rs_commit CHECK (REGEXP_LIKE(commit_sha, '^[0-9a-f]{40}$', 'c')),
    CONSTRAINT ck_rs_blob CHECK (REGEXP_LIKE(blob_sha, '^[0-9a-f]{40}$', 'c')),
    CONSTRAINT ck_rs_content CHECK (REGEXP_LIKE(content_hash, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT ck_rs_key CHECK (REGEXP_LIKE(snapshot_key, '^[0-9a-f]{64}$', 'c'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE search_snapshots (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plan_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    repository_id BIGINT UNSIGNED NOT NULL,
    snapshot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    usage_type VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    used_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ss_run_repo_snapshot_usage UNIQUE (run_id, repository_id, snapshot_id, usage_type),
    INDEX ss_plan_run (plan_id, run_id),
    INDEX ss_snapshot_repo (snapshot_id, repository_id),
    CONSTRAINT fk_ss_plan_run FOREIGN KEY (plan_id, run_id) REFERENCES search_plans (plan_id, run_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_ss_snapshot_repo FOREIGN KEY (snapshot_id, repository_id) REFERENCES repository_snapshots (snapshot_id, repository_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_ss_usage CHECK (usage_type IN ('candidate_review', 'evidence_retrieval', 'deep_analysis'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
