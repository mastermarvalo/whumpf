"""Alembic environment.

The URL comes from app settings (DATABASE_URL). Two overrides, both of which
only change the database *name* on that URL:

* ``alembic -x dbname=whumpf_schemacheck ...`` — used by scripts/schema-diff.sh
  to build a throwaway database without putting the password on a command line.
* ``config.attributes["database_url"]`` — used by tests/conftest.py.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or get_settings().database_url
    dbname = context.get_x_argument(as_dictionary=True).get("dbname")
    if dbname:
        url = make_url(url).set(database=dbname).render_as_string(hide_password=False)
    return url


def include_object(obj, name, obj_type, reflected, compare_to) -> bool:
    # Tables that exist in the DB but not in our models belong to PostGIS
    # (spatial_ref_sys) or Alembic itself; never autogenerate drops for them.
    if obj_type == "table" and reflected and compare_to is None:
        return False
    return alembic_helpers.include_object(obj, name, obj_type, reflected, compare_to)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        render_item=alembic_helpers.render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            process_revision_directives=alembic_helpers.writer,
            render_item=alembic_helpers.render_item,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
