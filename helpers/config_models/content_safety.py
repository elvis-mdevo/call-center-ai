from typing import List
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings


class ContentSafetyModel(BaseSettings):
    access_key: SecretStr
    blocklists: List[str]
    category_hate_score: int = Field(default=0, ge=0, le=7)
    category_self_harm_score: int = Field(default=1, ge=0, le=7)
    category_sexual_score: int = Field(default=2, ge=0, le=7)
    category_violence_score: int = Field(default=0, ge=0, le=7)
    endpoint: str
