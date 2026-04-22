from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, Column, String, Integer, DateTime, desc
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class UserModel(Base):
    __tablename__ = 'users'
    user_id = Column(String, primary_key=True)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    first_seen = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class InteractionModel(Base):
    __tablename__ = 'interactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    action_type = Column(String)
    action_detail = Column(String)
    timestamp = Column(DateTime, default=datetime.now)

class GalleryPost(Base):
    __tablename__ = 'gallery_posts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String, nullable=False)
    caption = Column(String)
    timestamp = Column(DateTime, default=datetime.now)

class AppDB:
    def __init__(self, db_url="sqlite:///app_data.db"):
        self.engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def upsert_user(self, user_id: str, sender_obj: Any):
        if not sender_obj: return
        with self.Session() as session:
            user = session.query(UserModel).filter_by(user_id=user_id).first()
            if not user:
                user = UserModel(
                    user_id=user_id,
                    username=getattr(sender_obj, "username", ""),
                    first_name=getattr(sender_obj, "first_name", ""),
                    last_name=getattr(sender_obj, "last_name", "")
                )
                session.add(user)
            else:
                user.username = getattr(sender_obj, "username", "")
                user.first_name = getattr(sender_obj, "first_name", "")
                user.last_name = getattr(sender_obj, "last_name", "")
            session.commit()

    def log_interaction(self, user_id: str, action_type: str, action_detail: str = ""):
        with self.Session() as session:
            session.add(InteractionModel(
                user_id=user_id,
                action_type=action_type,
                action_detail=action_detail
            ))
            session.commit()

    def add_gallery_post(self, file_id: str, caption: str):
        with self.Session() as session:
            post = GalleryPost(file_id=file_id, caption=caption)
            session.add(post)
            session.commit()

    def get_latest_gallery_posts(self, limit=3):
        with self.Session() as session:
            return session.query(GalleryPost).order_by(desc(GalleryPost.timestamp)).limit(limit).all()