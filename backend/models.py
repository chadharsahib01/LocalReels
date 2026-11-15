from sqlalchemy import Column, String, Integer
from .database import Base

class Video(Base):
    __tablename__ = "videos"

    id = Column(String, primary_key=True, index=True)
    payload = Column(String) 
    status = Column(String)
    ordinal = Column(Integer)