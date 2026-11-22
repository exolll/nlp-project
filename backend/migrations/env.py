"""Offline SQL is credential-free; live migrations require explicit opt-in."""
import os

from alembic import context


def run_migrations_offline():
    # Offline rendering must not load credentials or depend on live model definitions.
    context.configure(
        url='mysql+pymysql://',
        target_metadata=None,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    if os.environ.get('PROJECTPILOT_ALLOW_MIGRATIONS') != '1':
        raise RuntimeError(
            'Online migrations are disabled. Review offline SQL first; '
            'set PROJECTPILOT_ALLOW_MIGRATIONS=1 only for an approved migration.'
        )

    from app.persistence.search_database import SearchDatabaseSettings, create_search_engine
    from app.persistence.search_models import SearchBase

    engine = create_search_engine(SearchDatabaseSettings())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=SearchBase.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
