import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.persistence.search_database import SearchDatabaseSettings, create_search_engine, search_transaction


@pytest.fixture(autouse=True)
def isolated_database_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.upper().startswith('MYSQL_') or key.upper() == 'ENVIRONMENT':
            monkeypatch.delenv(key)


def configuration(**overrides):
    return SearchDatabaseSettings(_env_file=None, **{
        'environment': 'test', 'mysql_user': 'local-test',
        'mysql_database': 'isolated_test', 'mysql_password': 'test-only:@/%secret',
        **overrides,
    })


def test_configuration_is_explicit_and_redacts_password():
    settings = configuration()
    assert settings.mysql_password.get_secret_value() not in repr(settings)
    with pytest.raises(ValidationError):
        SearchDatabaseSettings(_env_file=None)
    with pytest.raises(ValidationError):
        configuration(mysql_password='')


@pytest.mark.parametrize('field,value', [
    ('mysql_port', 0), ('mysql_port', 65536), ('mysql_pool_size', 0),
    ('mysql_pool_size', 11), ('mysql_connect_timeout_seconds', 0),
    ('mysql_read_timeout_seconds', 121), ('mysql_write_timeout_seconds', 0),
])
def test_bounded_configuration(field, value):
    with pytest.raises(ValidationError):
        configuration(**{field: value})


@pytest.mark.parametrize('changes', [
    {'environment': 'production'}, {'mysql_host': 'db.internal'},
])
def test_nonlocal_connections_require_tls(changes):
    with pytest.raises(ValidationError):
        configuration(**changes)
    settings = configuration(**changes, mysql_ssl_ca=Path('test-ca.pem'))
    assert settings.mysql_ssl_ca == Path('test-ca.pem')


def test_engine_creation_does_not_connect(monkeypatch):
    import pymysql

    def forbidden(*args, **kwargs):
        pytest.fail('Engine creation must not connect to MySQL')

    monkeypatch.setattr(pymysql, 'connect', forbidden)
    engine = create_search_engine(configuration())
    try:
        assert engine.url.drivername == 'mysql+pymysql'
        assert engine.url.database == 'isolated_test'
        assert engine.url.password == 'test-only:@/%secret'
        assert engine.url.password not in str(engine.url)
        assert engine.hide_parameters
        assert engine.echo is False
        assert engine.pool.size() == 3
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_engine_options_are_safe_and_utc(monkeypatch):
    import sqlalchemy

    factory = MagicMock()
    monkeypatch.setattr(sqlalchemy, 'create_engine', factory)
    settings = configuration(mysql_host='db.internal', mysql_ssl_ca=Path('test-ca.pem'))
    create_search_engine(settings)
    url = factory.call_args.args[0]
    options = factory.call_args.kwargs
    assert url.query == {'charset': 'utf8mb4'}
    assert options['max_overflow'] == 0
    assert options['pool_pre_ping'] is True
    assert options['connect_args'] == {
        'connect_timeout': 5, 'read_timeout': 15, 'write_timeout': 15,
        'init_command': "SET time_zone = '+00:00'",
        'ssl': {'ca': 'test-ca.pem', 'check_hostname': True},
    }


def test_each_transaction_has_an_independent_session(monkeypatch):
    import sqlalchemy.orm

    first, second = MagicMock(), MagicMock()
    first.__enter__.return_value = first
    second.__enter__.return_value = second
    factory = MagicMock(side_effect=[first, second])
    monkeypatch.setattr(sqlalchemy.orm, 'Session', factory)
    engine = object()
    with search_transaction(engine) as one:
        assert one is first
    with search_transaction(engine) as two:
        assert two is second
    assert factory.call_count == 2
    first.begin.assert_called_once_with()
    second.begin.assert_called_once_with()
    first.__exit__.assert_called_once_with(None, None, None)


def test_transaction_failure_propagates_without_retry(monkeypatch):
    import sqlalchemy.orm

    session = MagicMock()
    session.__enter__.return_value = session
    factory = MagicMock(return_value=session)
    monkeypatch.setattr(sqlalchemy.orm, 'Session', factory)
    with pytest.raises(RuntimeError, match='test failure'):
        with search_transaction(object()):
            raise RuntimeError('test failure')
    assert factory.call_count == 1
    assert session.begin.return_value.__exit__.call_args.args[0] is RuntimeError
