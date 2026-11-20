"""Application settings, loaded from environment variables / `.env`.

One typed object holds all config. Nothing else in the app reads os.environ.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AMLens"
    environment: Literal["local", "ci", "staging", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Postgres (app DB: alerts, cases, RAG index, job queue) ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "amlens"
    postgres_user: str = "amlens"
    postgres_password: SecretStr = SecretStr("amlens")

    # --- MS SQL (bank transaction warehouse, read-only) ---
    mssql_host: str = "localhost"
    mssql_port: int = 1433
    mssql_db: str = "master"
    mssql_user: str = "sa"
    mssql_password: SecretStr = SecretStr("change-me")
    mssql_driver: str = "ODBC Driver 18 for SQL Server"
    # The warehouse database created by scripts/load_mssql.py (ADR 0001).
    mssql_warehouse_db: str = "amlens_warehouse"
    # Read-only login for the text-to-SQL tool: SELECT on schema `bank` only, no access to `labels`.
    mssql_reader_user: str = "amlens_reader"
    mssql_reader_password: SecretStr = SecretStr("change-me")

    # --- Health checks ---
    health_check_timeout_s: float = 3.0

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy async URL for Postgres (asyncpg driver)."""
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def mssql_odbc_conn_str(self) -> str:
        """Raw ODBC connection string for MS SQL (used by aioodbc / pyodbc)."""
        return self.mssql_conn_str(self.mssql_db)

    def mssql_conn_str(
        self, database: str, user: str | None = None, password: SecretStr | None = None
    ) -> str:
        """ODBC connection string for any database / login (defaults: the admin login)."""
        pw = (password or self.mssql_password).get_secret_value()
        return (
            f"DRIVER={{{self.mssql_driver}}};"
            f"SERVER={self.mssql_host},{self.mssql_port};"
            f"DATABASE={database};"
            f"UID={user or self.mssql_user};PWD={pw};"
            "TrustServerCertificate=yes;Encrypt=yes;"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Use as a FastAPI dependency or call directly."""
    return Settings()
