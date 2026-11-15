from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "LocalReels New"
    SECRET_KEY: str = "nayi_app_secret_123"
    MAX_PARALLEL_JOBS: int = 5 
    DOWNLOAD_KIB: int = 1024
    
    class Config:
        env_file = ".env"

settings = Settings()