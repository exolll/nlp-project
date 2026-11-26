from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore')
    environment: Literal['development', 'production', 'test'] = 'development'
    max_session_tasks: int = Field(100, gt=0)
    storage_backend: Literal['local', 's3'] = 'local'
    local_storage_directory: Path = PROJECT_ROOT / 'data' / 'objects'
    chroma_path: Path = PROJECT_ROOT / 'data' / 'chroma'
    s3_endpoint_url: str | None = None
    s3_region: str = 'us-east-1'
    s3_bucket: str = ''
    s3_access_key_id: str = ''
    s3_secret_access_key: str = ''
    cors_origins: list[str] = ['http://127.0.0.1:5173', 'http://localhost:5173']
    max_file_bytes: int = Field(10_000_000, gt=0)
    max_request_bytes: int = Field(11 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(30, gt=0)
    max_primary_characters: int = Field(30_000, gt=0)
    max_supplement_characters: int = Field(2_000, gt=0)
    max_zip_bytes: int = Field(50 * 1024 * 1024, gt=0)
    max_zip_entries: int = Field(1000, gt=0)
    max_zip_ratio: float = Field(100, gt=0)
    parse_timeout_seconds: float = Field(15, gt=0)
    session_hours: int = Field(24, gt=0)
    session_requests_per_minute: int = Field(10, gt=0)
    write_requests_per_minute: int = Field(10, gt=0)
    max_processing_tasks: int = Field(2, gt=0)
    extraction_timeout_seconds: float = Field(90, gt=0, le=600)
    extraction_max_output_tokens: int = Field(6000, gt=0)
    extraction_max_input_tokens: int = Field(20000, gt=0)
    extraction_max_calls: int = Field(5, gt=0)
    max_requirement_revisions: int = Field(50, gt=0, le=50)
    max_task_extractions: int = Field(10, gt=0, le=10)

    @model_validator(mode='after')
    def deployment_safety(self):
        if self.environment == 'production' and self.storage_backend != 's3':
            raise ValueError('生产环境必须配置托管对象存储，不可使用本地适配器。')
        if '*' in self.cors_origins:
            raise ValueError('必须配置具体的前端来源。')
        return self

    def parser_limits(self):
        return self.model_dump(include={
            'max_file_bytes', 'max_pdf_pages', 'max_primary_characters',
            'max_supplement_characters', 'max_zip_bytes', 'max_zip_entries', 'max_zip_ratio',
        })


class ExtractionSettings(BaseSettings):
    """Loaded lazily: missing/invalid provider configuration cannot break input APIs."""
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore', hide_input_in_errors=True)
    deepseek_api_key: SecretStr = SecretStr('')
    deepseek_base_url: Literal['https://api.deepseek.com'] = 'https://api.deepseek.com'
    deepseek_model: Literal['deepseek-flash'] = 'deepseek-flash'


class SiliconFlowSettings(BaseSettings):
    """Embedding transport configuration; the key is never logged or echoed."""
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore', hide_input_in_errors=True)
    siliconflow_api_key: SecretStr = SecretStr('')
    siliconflow_base_url: str = 'https://api.siliconflow.cn/v1'
    embedding_provider: Literal['siliconflow'] = 'siliconflow'
    embedding_model: Literal['BAAI/bge-m3'] = 'BAAI/bge-m3'
    embedding_dimension: int = Field(default=1024, ge=1, le=8192)
    embedding_batch_size: int = Field(default=16, ge=1, le=32)
    embedding_max_batches: int = Field(default=32, ge=1, le=128)
    embedding_max_chunks_per_run: int = Field(default=512, ge=1, le=1000)
    embedding_max_tokens_per_run: int = Field(default=262144, ge=1)
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=120)
    embedding_max_retries: int = Field(default=1, ge=0, le=2)
    embedding_enabled: bool = True

    @model_validator(mode='after')
    def require_key_when_enabled(self):
        if self.embedding_dimension != 1024:
            raise ValueError('BAAI/bge-m3 的向量维度必须是 1024。')
        if self.embedding_enabled and not self.siliconflow_api_key.get_secret_value().strip():
            raise ValueError('缺少 SILICONFLOW_API_KEY：Embedding 已启用但未配置密钥。')
        return self

    def version(self):
        return f'{self.embedding_model}-{self.embedding_dimension}-v1'


def validate_embedding_config():
    """Startup gate: fail clearly instead of failing on the first paid index call."""
    try:
        return SiliconFlowSettings()
    except ValueError:
        raise RuntimeError(
            'Embedding 配置不完整：请设置 SILICONFLOW_API_KEY；'
            '若只需调试输入，请显式设置 EMBEDDING_ENABLED=false。') from None
