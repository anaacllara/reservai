from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RESERVAI_")

    redis_url: str = "redis://localhost:6379/0"
    cassandra_hosts: str = "localhost"  # separados por vírgula
    cassandra_keyspace: str = "reservai"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "reservai"

    fuso: str = "America/Sao_Paulo"
    lock_ttl_s: int = 30  # Marco 1, 2.2a
    cache_disponibilidade_ttl_s: int = 300  # Marco 1, 2.2b
    limite_reservas_ativas: int = 2  # RF06
    dias_agenda_aberta: int = 7  # Marco 1, 1.3


@lru_cache
def get_settings() -> Settings:
    return Settings()
