from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine
from sqlalchemy.orm import relationship, sessionmaker
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, JSON, text, Boolean

SQLALCHEMY_DATABASE_URL = "sqlite:///./sfbt_ollama.db"

engine = create_engine(
	SQLALCHEMY_DATABASE_URL,
	connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def _ensure_guardian_phone_column():
	try:
		with engine.connect() as conn:
			result = conn.execute(text("PRAGMA table_info(children)"))
			columns = [row[1] for row in result]
			if "guardian_phone" not in columns:
				conn.execute(text("ALTER TABLE children ADD COLUMN guardian_phone VARCHAR(30)"))
			if "account" not in columns:
				conn.execute(text("ALTER TABLE children ADD COLUMN account VARCHAR(50)"))
			if "password" not in columns:
				conn.execute(text("ALTER TABLE children ADD COLUMN password VARCHAR(128)"))
	except Exception as exc:
		print("⚠️ 无法自动添加 guardian_phone 列：", exc)


_ensure_guardian_phone_column()


class SFBTKnowledge(Base):
	__tablename__ = "sfbt_knowledge"
	id = Column(Integer, primary_key=True, autoincrement=True)
	title = Column(String(200))
	source_url = Column(String(300))
	content = Column(Text)
	embedding = Column(JSON, nullable=True)
	last_update = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Child(Base):
	__tablename__ = "children"
	id = Column(Integer, primary_key=True, autoincrement=True)
	name = Column(String(50), nullable=False)
	account = Column(String(50), nullable=True, unique=True)
	password = Column(String(128), nullable=True)
	age = Column(Integer, nullable=True)
	guardian = Column(String(100), nullable=True)
	guardian_phone = Column(String(30), nullable=True)
	case_notes = Column(Text, nullable=True)
	stage = Column(String(50), default="目标设定阶段")
	progress_score = Column(Float, default=0.0)
	last_update = Column(DateTime, default=lambda: datetime.now(timezone.utc))

	interactions = relationship("Interaction", back_populates="child")
	conversations = relationship("Conversation", back_populates="child")

	# 可选：将来若需要账户体系，可在此关联到用户表
	# user_id = Column(Integer, nullable=True)


class Interaction(Base):
	__tablename__ = "interactions"
	id = Column(Integer, primary_key=True, autoincrement=True)
	child_id = Column(Integer, ForeignKey("children.id"))
	conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
	user_input = Column(Text)
	bot_response = Column(Text)
	timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
	child = relationship("Child", back_populates="interactions")


class Conversation(Base):
	__tablename__ = "conversations"
	id = Column(Integer, primary_key=True, autoincrement=True)
	child_id = Column(Integer, ForeignKey("children.id"))
	title = Column(String(200), nullable=True)
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
	child = relationship("Child", back_populates="conversations")


class CrisisAlert(Base):
	__tablename__ = "crisis_alerts"
	id = Column(Integer, primary_key=True, autoincrement=True)
	child_id = Column(Integer, ForeignKey("children.id"), nullable=False)
	interaction_id = Column(Integer, ForeignKey("interactions.id"), nullable=False)
	flags = Column(JSON, nullable=True)
	summary = Column(String(200), nullable=True)
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
	reviewed = Column(Boolean, default=False)
	reviewed_at = Column(DateTime, nullable=True)
	notes = Column(Text, nullable=True)

try:
	CrisisAlert.__table__.create(bind=engine, checkfirst=True)
except Exception as _e:
	print("⚠️ 创建 crisis_alerts 表失败：", _e)