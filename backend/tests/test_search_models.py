"""Static schema and Alembic checks: no database or network is permitted."""
import builtins
import importlib.util
import io
from pathlib import Path
import re
import socket

from alembic import command
from alembic.config import Config
import pymysql
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.persistence.search_models import SearchBase

BACKEND = Path(__file__).resolve().parents[1]
REVISION = BACKEND / 'migrations/versions/20261121_01_search_tables.py'
TABLES = {
    'search_plans', 'search_groups', 'search_queries', 'repositories',
    'repository_snapshots', 'search_snapshots',
}
DIALECT = mysql.dialect()


@pytest.fixture(autouse=True)
def forbid_database_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Schema tests must never connect to a database or network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)
    monkeypatch.setattr(pymysql, 'connect', forbidden)
    monkeypatch.setattr(pymysql.connections.Connection, 'connect', forbidden)
    monkeypatch.delenv('PROJECTPILOT_ALLOW_MIGRATIONS', raising=False)


def render(direction='upgrade'):
    output = io.StringIO()
    config = Config(str(BACKEND / 'alembic.ini'), output_buffer=output)
    if direction == 'upgrade':
        command.upgrade(config, 'head', sql=True)
    else:
        command.downgrade(config, '20261121_01:base', sql=True)
    return output.getvalue()


def load_revision():
    spec = importlib.util.spec_from_file_location('frozen_search_revision', REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frozen_metadata(monkeypatch):
    metadata = sa.MetaData()

    class CaptureOperations:
        @staticmethod
        def create_table(name, *columns_and_constraints, **options):
            return sa.Table(name, metadata, *columns_and_constraints, **options)

        @staticmethod
        def create_index(name, table_name, columns, **options):
            sa.Index(name, *(metadata.tables[table_name].c[c] for c in columns), **options)

    revision = load_revision()
    monkeypatch.setattr(revision, 'op', CaptureOperations)
    revision.upgrade()
    return metadata


def compile_metadata(metadata):
    statements = []
    for table in metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=DIALECT)).strip() + ';')
        statements.extend(str(CreateIndex(index).compile(dialect=DIALECT)) + ';'
                          for index in table.indexes)
    return '\n'.join(statements)


def normalize(fragment):
    # Normalize only presentation outside string literals; CHECK literals stay exact.
    parts = re.split(r"('(?:''|[^'])*')", fragment)
    for i in range(0, len(parts), 2):
        text = parts[i].replace('`', '').lower()
        text = re.sub(r'collate\s+"([^"]+)"', r'collate \1', text)
        text = re.sub(r'\bboolean\b', 'bool', text)
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'\s*([(),=])\s*', r'\1', text)
        # Explicit utf8mb4 is equivalent to the declared table default.
        text = text.replace(' character set utf8mb4', '')
        parts[i] = text
    return ''.join(parts).strip()


def ddl_structure(sql):
    """Parse DDL by line because composite keys and CHECKs contain nested commas."""
    result = {}
    for match in re.finditer(r'CREATE TABLE\s+`?(\w+)`?\s*\((.*?)\n\)\s*([^;]*);', sql, re.S | re.I):
        name, body, options = match.groups()
        if name == 'alembic_version':
            continue
        entries, indexes = set(), set()
        for line in body.splitlines():
            line = line.strip().rstrip(',')
            if not line:
                continue
            if re.match(r'INDEX\s', line, re.I):
                indexes.add(normalize(re.sub(r'^INDEX\s+', '', line, flags=re.I)))
                continue
            if not re.match(r'(CONSTRAINT|PRIMARY KEY|UNIQUE|FOREIGN KEY|CHECK)\b', line, re.I):
                if not re.search(r'\bNOT NULL\b', line, re.I):
                    line = re.sub(r'\s+NULL$', '', line, flags=re.I) + ' NULL'
            entries.add(normalize(line))
        normalized_options = normalize(options)
        opts = {key: re.search(pattern, normalized_options).group(1) for key, pattern in {
            'engine': r'engine=(\w+)', 'charset': r'charset=(\w+)',
            'collate': r'collate[= ](\w+)',
        }.items()}
        result[name] = {'entries': entries, 'indexes': indexes, 'options': opts}
    for name, table, columns in re.findall(r'CREATE INDEX\s+(\w+)\s+ON\s+(\w+)\s*(\([^;]+\));', sql, re.I):
        result[table]['indexes'].add(normalize(name + ' ' + columns))
    return result


def test_six_table_metadata_and_mysql_types():
    metadata = SearchBase.metadata
    assert set(metadata.tables) == TABLES
    assert sum(len(t.columns) for t in metadata.tables.values()) == 56
    for table in metadata.tables.values():
        assert len(table.primary_key.columns) == 1
        assert dict(table.kwargs) == {
            'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4',
            'mysql_collate': 'utf8mb4_unicode_ci',
        }
        for column in table.columns:
            assert column.default is None and column.server_default is None
            if isinstance(column.type, mysql.BIGINT):
                assert column.type.unsigned
            if isinstance(column.type, mysql.DATETIME):
                assert column.type.fsp == 6
            if isinstance(column.type, mysql.CHAR):
                assert column.type.charset == 'ascii'
                assert column.type.collation == 'ascii_bin'
            if isinstance(column.type, mysql.JSON):
                assert column.type.none_as_null is True
                assert column.type.bind_processor(DIALECT)(None) is None
        for fk in table.foreign_key_constraints:
            assert fk.ondelete == fk.onupdate == 'RESTRICT'
    assert metadata.tables['repositories'].c.repository_id.autoincrement is False
    assert metadata.tables['repository_snapshots'].c.readme_path.type.collation == 'utf8mb4_0900_bin'
    for mapper in SearchBase.registry.mappers:
        assert all('delete' not in rel.cascade and 'delete-orphan' not in rel.cascade
                   for rel in mapper.relationships)


def test_composite_foreign_keys_have_ordered_type_matching_unique_parents():
    metadata = SearchBase.metadata
    composite = {fk.name: fk for t in metadata.tables.values()
                 for fk in t.foreign_key_constraints if len(fk.elements) > 1}
    assert set(composite) == {'fk_ss_plan_run', 'fk_ss_snapshot_repo'}
    expected = {
        'fk_ss_plan_run': ('search_plans', ['plan_id', 'run_id']),
        'fk_ss_snapshot_repo': ('repository_snapshots', ['snapshot_id', 'repository_id']),
    }
    for name, fk in composite.items():
        parent_name, columns = expected[name]
        assert list(fk.column_keys) == columns
        assert [e.column.name for e in fk.elements] == columns
        assert fk.referred_table.name == parent_name
        assert any(isinstance(c, sa.UniqueConstraint) and list(c.columns.keys()) == columns
                   for c in fk.referred_table.constraints)
        for element in fk.elements:
            assert element.parent.type.compile(dialect=DIALECT) == element.column.type.compile(dialect=DIALECT)


def test_frozen_revision_matches_models_and_manual_sql(monkeypatch):
    frozen = frozen_metadata(monkeypatch)
    model_structure = ddl_structure(compile_metadata(SearchBase.metadata))
    assert set(model_structure) == TABLES
    assert ddl_structure(compile_metadata(frozen)) == model_structure
    assert ddl_structure(render()) == model_structure
    manual_sql = (BACKEND.parent / 'docs/ProjectPilot_MySQL_Create_Tables.sql').read_text(encoding='utf-8')
    assert ddl_structure(manual_sql) == model_structure
    # JSON None policy is not visible in DDL, so compare the captured definitions too.
    for table in frozen.tables.values():
        for column in table.columns:
            if isinstance(column.type, mysql.JSON):
                assert column.type.none_as_null is True


def test_offline_upgrade_and_downgrade_do_not_load_settings_or_models(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {'app.persistence.search_database', 'app.persistence.search_models', 'dotenv'}:
            raise AssertionError('Offline revisions must not load live application configuration/models')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    upgrade = render()
    downgrade = render('downgrade')
    assert set(ddl_structure(upgrade)) == TABLES
    created = re.findall(r'CREATE TABLE (\w+)', upgrade)
    dropped = re.findall(r'DROP TABLE (\w+)', downgrade)
    assert [name for name in dropped if name != 'alembic_version'] == list(reversed(
        [name for name in created if name != 'alembic_version']))
    assert 'CASCADE' not in upgrade + downgrade
    assert 'AUTO_INCREMENT' not in upgrade


@pytest.mark.parametrize('value', [None, '', '0', 'true', 'yes', ' 1'])
def test_online_requires_exact_explicit_opt_in(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('PROJECTPILOT_ALLOW_MIGRATIONS', value)
    with pytest.raises(RuntimeError, match='PROJECTPILOT_ALLOW_MIGRATIONS=1'):
        command.upgrade(Config(str(BACKEND / 'alembic.ini')), 'head')


def test_future_model_changes_cannot_change_historical_sql(monkeypatch):
    before = render()
    monkeypatch.setattr(SearchBase, 'metadata', sa.MetaData())
    table = next(iter(SearchBase.registry.mappers)).local_table
    column = next(iter(table.columns))
    monkeypatch.setattr(column, 'type', sa.String(17))
    assert render() == before
    source = REVISION.read_text(encoding='utf-8')
    assert 'app.persistence' not in source
    assert 'create_all' not in source and 'drop_all' not in source
