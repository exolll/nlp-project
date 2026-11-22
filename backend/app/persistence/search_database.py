from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SearchDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / '.env',
        extra='ignore', hide_input_in_errors=True,
    )

    environment: Literal['development', 'production', 'test'] = 'development'
    mysql_host: str = Field(default='127.0.0.1', min_length=1)
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = Field(min_length=1)
    mysql_user: str = Field(min_length=1)
    mysql_password: SecretStr
    mysql_ssl_ca: Path | None = None
    mysql_pool_size: int = Field(default=3, ge=1, le=10)
    mysql_pool_timeout_seconds: int = Field(default=5, ge=1, le=60)
    mysql_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    mysql_read_timeout_seconds: int = Field(default=15, ge=1, le=120)
    mysql_write_timeout_seconds: int = Field(default=15, ge=1, le=120)

    @model_validator(mode='after')
    def require_secure_connection(self):
        if not self.mysql_password.get_secret_value():
            raise ValueError('必须配置非空 MySQL 密码。')
        if (self.environment == 'production' or self.mysql_host not in {'localhost', '127.0.0.1', '::1'}) and self.mysql_ssl_ca is None:
            raise ValueError('远程或生产 MySQL 连接必须配置受信任 CA。')
        return self


def create_search_engine(settings: SearchDatabaseSettings):
    from sqlalchemy import URL, create_engine

    connect_args = {
        'connect_timeout': settings.mysql_connect_timeout_seconds,
        'read_timeout': settings.mysql_read_timeout_seconds,
        'write_timeout': settings.mysql_write_timeout_seconds,
        'init_command': "SET time_zone = '+00:00'",
    }
    if settings.mysql_ssl_ca is not None:
        connect_args['ssl'] = {'ca': str(settings.mysql_ssl_ca), 'check_hostname': True}
    return create_engine(
        URL.create(
            'mysql+pymysql', username=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            host=settings.mysql_host, port=settings.mysql_port,
            database=settings.mysql_database, query={'charset': 'utf8mb4'},
        ),
        connect_args=connect_args, echo=False, hide_parameters=True,
        pool_pre_ping=True, pool_size=settings.mysql_pool_size, max_overflow=0,
        pool_timeout=settings.mysql_pool_timeout_seconds, pool_recycle=1800,
    )


@contextmanager
def search_transaction(engine):
    from sqlalchemy.orm import Session

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            yield session
