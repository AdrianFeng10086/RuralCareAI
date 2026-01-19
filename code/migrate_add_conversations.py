#!/usr/bin/env python3
"""迁移脚本：添加 conversations 表与 interaction.conversation_id 列，并把现有交互归入默认会话（code 包内部使用）。"""
import os
import shutil
import sqlite3
from sqlalchemy import inspect

from code.db_models import SQLALCHEMY_DATABASE_URL, engine, Base, SessionLocal, Child, Conversation, Interaction


def get_sqlite_path(url: str) -> str:
	if url.startswith('sqlite:///'):
		return url.replace('sqlite:///', '')
	return url


def backup_db(db_path: str):
	bak = db_path + '.bak'
	if os.path.exists(bak):
		print(f'备份文件已存在：{bak}, 会覆盖')
		os.remove(bak)
	shutil.copy2(db_path, bak)
	print(f'已备份 {db_path} -> {bak}')


def add_column_if_missing(db_path: str):
	conn = sqlite3.connect(db_path)
	cur = conn.cursor()
	cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'")
	if not cur.fetchone():
		print('数据库中没有 interactions 表，跳过添加列')
		conn.close()
		return
	cur.execute("PRAGMA table_info(interactions)")
	cols = [r[1] for r in cur.fetchall()]
	if 'conversation_id' in cols:
		print('列 conversation_id 已存在，跳过 ALTER')
	else:
		print('向 interactions 表添加 conversation_id 列...')
		cur.execute('ALTER TABLE interactions ADD COLUMN conversation_id INTEGER')
		conn.commit()
		print('已添加列 conversation_id')
	conn.close()


def create_conversation_table_and_assign():
	Base.metadata.create_all(bind=engine)
	print('已确保 ORM 表结构创建（如果缺失则已创建）')
	session = SessionLocal()
	try:
		children = session.query(Child).all()
		for child in children:
			conv = Conversation(child_id=child.id, title='历史会话')
			session.add(conv)
			session.commit()
			print(f'为 child id={child.id} 创建会话 id={conv.id}')
			session.query(Interaction).filter(Interaction.child_id == child.id, Interaction.conversation_id == None).update({Interaction.conversation_id: conv.id})
			session.commit()
			print(f'已把 child id={child.id} 的历史交互分配到会话 id={conv.id}')
	finally:
		session.close()


def main():
	db_path = get_sqlite_path(SQLALCHEMY_DATABASE_URL)
	if not os.path.exists(db_path):
		print(f'数据库文件不存在：{db_path}，请确认路径或先运行程序以创建数据库。')
		return
	print('开始迁移...')
	backup_db(db_path)
	add_column_if_missing(db_path)
	create_conversation_table_and_assign()
	print('迁移完成。请重启应用并再次测试。')


if __name__ == '__main__':
	main()