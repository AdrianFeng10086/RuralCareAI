from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import uvicorn
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
import threading
import queue
import json
import time

from .db_models import SessionLocal, Child, Interaction, SFBTKnowledge, Conversation, CrisisAlert
from .auth import ADMIN_COOKIE_NAME, ADMIN_USERNAME, ADMIN_PASSWORD, require_admin, AdminAuthMiddleware
from .dialogue_manager import SFBTDialogueManager
try:
	from .rag_module import SFBTRAG
except Exception:
	class SFBTRAG:
		def __init__(self):
			pass
		def retrieve(self, query, top_k=3):
			return ''
from PyPDF2 import PdfReader
from .alert_bus import subscribe as alert_subscribe, unsubscribe as alert_unsubscribe

@asynccontextmanager
async def lifespan(app: FastAPI):
	try:
		with SessionLocal() as db:
			result = _sync_uploads_knowledge(db)
			_cleanup_reserved_children(db)
		if result.get("added") or result.get("removed"):
			try:
				rag = SFBTRAG()
				rag.build_vectorstore()
			except Exception:
				pass
	except Exception:
		pass
	yield


app = FastAPI(title="SFBT 管理后台与 API", lifespan=lifespan)
app.add_middleware(AdminAuthMiddleware)

# 资源路径：使用项目根目录的 templates 与 static（src/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def get_db():
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


def _extract_text_from_path(path: Path) -> str:
	text = ""
	try:
		if path.suffix.lower() == ".pdf":
			reader = PdfReader(path)
			pages = []
			for p in reader.pages:
				try:
					pages.append(p.extract_text() or "")
				except Exception:
					pages.append("")
			text = "\n\n".join(pages)
		else:
			text = path.read_text(encoding="utf-8", errors="ignore")
	except Exception:
		text = ""
	return text


def _sync_uploads_knowledge(db: Session) -> dict:
	uploads_dir = BASE_DIR / "uploads" / "knowledge"
	uploads_dir_resolved = str(uploads_dir.resolve())
	rows = db.query(SFBTKnowledge).all()
	existing = {k.source_url for k in rows if k.source_url}
	added = 0
	removed = 0
	if uploads_dir.exists():
		for item in uploads_dir.iterdir():
			if not item.is_file():
				continue
			full_path = str(item.resolve())
			if full_path in existing:
				continue
			text = _extract_text_from_path(item)
			k = SFBTKnowledge(title=item.name, source_url=full_path, content=text)
			db.add(k)
			added += 1
	for row in rows:
		if not row.source_url:
			continue
		try:
			code_path = Path(row.source_url).resolve()
			common = os.path.commonpath([str(code_path), uploads_dir_resolved])
			expected_root = uploads_dir_resolved
			exists = code_path.exists()
		except Exception:
			continue
		if common != expected_root:
			continue
		if not exists:
			db.delete(row)
			removed += 1
	if added or removed:
		db.commit()
	return {"added": added, "removed": removed}


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
	"""管理端登录页。"""
	return templates.TemplateResponse("login.html", {"request": request, "error": None, "mode": "admin"})


@app.post("/admin/login")
def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
	"""管理端登录逻辑（保持原有账号密码）。"""
	if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
		resp = RedirectResponse(url="/admin", status_code=303)
		resp.set_cookie(ADMIN_COOKIE_NAME, "ok", httponly=True, max_age=3600 * 8)
		return resp
	return templates.TemplateResponse("login.html", {"request": request, "error": "账号或密码错误", "mode": "admin"}, status_code=401)


@app.get("/admin/logout")
def admin_logout(next: str = None):
	target = next if (isinstance(next, str) and next.startswith('/')) else "/admin/login"
	resp = RedirectResponse(url=target, status_code=303)
	resp.delete_cookie(ADMIN_COOKIE_NAME)
	return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	children = db.query(Child).all()
	pending_alerts = db.query(CrisisAlert).filter(CrisisAlert.reviewed == False).count()
	knowledge_count = db.query(SFBTKnowledge).count()
	return templates.TemplateResponse("index.html", {"request": request, "children": children, "knowledge_count": knowledge_count, "pending_alerts": pending_alerts})


@app.get("/api/admin/dashboard")
def api_admin_dashboard(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""管理后台首页所需数据的 JSON 版本。

	返回：
	- children: 儿童列表（id, name, age, stage, guardian, guardian_phone）
	- knowledge_count: 知识条目数量
	- pending_alerts: 未审查预警数量
	"""
	children = db.query(Child).all()
	items = []
	for c in children:
		items.append({
			"id": c.id,
			"name": c.name,
			"age": c.age,
			"stage": c.stage,
			"guardian": c.guardian,
			"guardian_phone": c.guardian_phone,
		})
	pending_alerts = db.query(CrisisAlert).filter(CrisisAlert.reviewed == False).count()
	knowledge_count = db.query(SFBTKnowledge).count()
	return JSONResponse({
		"children": items,
		"knowledge_count": knowledge_count,
		"pending_alerts": pending_alerts,
	})


@app.get("/api/admin/pending-alerts")
def api_admin_pending_alerts(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""仅返回未处理心理预警数量的轻量接口，便于高频轮询。"""
	count = db.query(CrisisAlert).filter(CrisisAlert.reviewed == False).count()
	return JSONResponse({"pending_alerts": count})


@app.get('/admin/alerts', response_class=HTMLResponse)
def admin_alerts(request: Request, q: str = None, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	alerts = db.query(CrisisAlert).order_by(CrisisAlert.created_at.desc()).limit(300).all()
	name_map = {}
	if alerts:
		child_ids = {a.child_id for a in alerts if a.child_id is not None}
		if child_ids:
			children = db.query(Child.id, Child.name).filter(Child.id.in_(child_ids)).all()
			name_map = {cid: cname for cid, cname in children}
			for a in alerts:
				try:
					setattr(a, 'child_name', name_map.get(a.child_id) or '未知')
				except Exception:
					setattr(a, 'child_name', '未知')
	if q:
		qlow = q.strip().lower()
		filtered = []
		for a in alerts:
			name = (getattr(a, 'child_name', '') or '')
			summary = (a.summary or '')
			text = f"{name}\n{summary}".lower()
			if qlow in text:
				filtered.append(a)
		alerts = filtered
	return templates.TemplateResponse('alerts.html', {"request": request, "alerts": alerts, "q": q or ''})


@app.get('/api/admin/alerts')
def api_admin_alerts(q: str = None, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""心理预警列表的 JSON 接口，支持按关键字简单过滤。"""
	alerts = db.query(CrisisAlert).order_by(CrisisAlert.created_at.desc()).limit(300).all()
	name_map = {}
	if alerts:
		child_ids = {a.child_id for a in alerts if a.child_id is not None}
		if child_ids:
			children = db.query(Child.id, Child.name).filter(Child.id.in_(child_ids)).all()
			name_map = {cid: cname for cid, cname in children}
	for a in alerts:
		setattr(a, 'child_name', name_map.get(a.child_id) or '未知')
	if q:
		qlow = q.strip().lower()
		filtered = []
		for a in alerts:
			name = (getattr(a, 'child_name', '') or '')
			summary = (a.summary or '')
			text = f"{name}\n{summary}".lower()
			if qlow in text:
				filtered.append(a)
		alerts = filtered
	items = []
	for a in alerts:
		items.append({
			"id": a.id,
			"child_id": a.child_id,
			"child_name": getattr(a, 'child_name', '未知'),
			"interaction_id": a.interaction_id,
			"flags": a.flags,
			"summary": a.summary,
			"created_at": a.created_at.isoformat() if a.created_at else None,
			"reviewed": a.reviewed,
			"reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
			"notes": a.notes,
		})
	return JSONResponse({"alerts": items})


@app.post('/admin/alerts/review')
def admin_alert_review(alert_id: int = Form(...), db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	alert = db.query(CrisisAlert).filter_by(id=alert_id).first()
	if not alert:
		return JSONResponse({'error': 'not found'}, status_code=404)
	alert.reviewed = True
	alert.reviewed_at = datetime.now(timezone.utc)
	db.commit()
	return RedirectResponse(url='/admin/alerts', status_code=303)


USER_COOKIE_NAME = "user_account"
GUEST_COOKIE_NAME = "guest_mode"
GUEST_NAME = "游客"


def _is_guest(request: Request) -> bool:
	return request.cookies.get(GUEST_COOKIE_NAME) == "1"


def _cleanup_reserved_children(db: Session) -> int:
	reserved_names = ("user", "匿名儿童")
	removed = 0
	rows = db.query(Child).filter(Child.name.in_(reserved_names)).all()
	for child in rows:
		if child.account:
			continue
		interactions = db.query(Interaction).filter_by(child_id=child.id).all()
		if interactions:
			if len(interactions) > 1:
				continue
			intro = interactions[0]
			if (intro.user_input or "").strip() not in ("（系统）开启对话", "(系统)开启对话"):
				continue
		try:
			db.query(CrisisAlert).filter_by(child_id=child.id).delete()
			db.query(Interaction).filter_by(child_id=child.id).delete()
			db.query(Conversation).filter_by(child_id=child.id).delete()
			db.delete(child)
			removed += 1
		except Exception:
			pass
	if removed:
		db.commit()
	return removed


@app.get("/", response_class=HTMLResponse)
def root_user_login(request: Request):
	"""默认进入用户端登录界面。"""
	# 如果已有用户 cookie，可以直接进入聊天页
	if request.cookies.get(USER_COOKIE_NAME) or _is_guest(request):
		return RedirectResponse(url="/user/chat", status_code=303)
	return templates.TemplateResponse("login.html", {"request": request, "error": None, "mode": "user"})


@app.get("/user/guest")
def user_guest_login():
	resp = RedirectResponse(url="/user/chat", status_code=303)
	resp.set_cookie(GUEST_COOKIE_NAME, "1", max_age=3600 * 24 * 30)
	resp.delete_cookie(USER_COOKIE_NAME)
	return resp

@app.post("/user/login")
def user_login(request: Request, account: str = Form(...), password: str = Form(""), db: Session = Depends(get_db)):
	"""用户端登录：使用管理端为儿童设置的账号和密码。

	- 前端展示的是儿童姓名，但实际登录使用的是 account + password。
	- 这里仅做简单明文校验，如需更安全可改用哈希。
	"""
	account = (account or "").strip()
	password = (password or "").strip()
	if not account:
		return templates.TemplateResponse("login.html", {"request": request, "error": "请输入账号", "mode": "user"}, status_code=400)
	child = db.query(Child).filter(Child.account == account).first()
	if not child or (child.password or "") != password:
		return templates.TemplateResponse("login.html", {"request": request, "error": "账号或密码错误", "mode": "user"}, status_code=401)
	resp = RedirectResponse(url="/user/chat", status_code=303)
	resp.set_cookie(USER_COOKIE_NAME, account, max_age=3600 * 24 * 30)
	resp.delete_cookie(GUEST_COOKIE_NAME)
	return resp


@app.get("/user/logout")
def user_logout():
	resp = RedirectResponse(url="/", status_code=303)
	resp.delete_cookie(USER_COOKIE_NAME)
	resp.delete_cookie(GUEST_COOKIE_NAME)
	return resp


@app.get("/user/chat", response_class=HTMLResponse)
def user_chat_page(request: Request, db: Session = Depends(get_db)):
	"""用户端聊天页面，需要先登录。"""
	if _is_guest(request):
		return templates.TemplateResponse("user_chat.html", {"request": request, "name": GUEST_NAME, "guest_mode": True, "intro": SFBTDialogueManager.get_intro_text()})
	account = request.cookies.get(USER_COOKIE_NAME)
	if not account:
		return RedirectResponse(url="/", status_code=303)
	child = db.query(Child).filter_by(account=account).first()
	if not child:
		return RedirectResponse(url="/", status_code=303)
	name = child.name
	return templates.TemplateResponse("user_chat.html", {"request": request, "name": name, "guest_mode": False, "intro": None})


@app.get("/children", response_class=HTMLResponse)
def children_page(request: Request, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	children = db.query(Child).all()
	return templates.TemplateResponse("children.html", {"request": request, "children": children})


@app.get("/children/new", response_class=HTMLResponse)
def new_child_page(request: Request, _: bool = Depends(require_admin)):
	"""单独的新建儿童页面。"""
	return templates.TemplateResponse("new_child.html", {"request": request})


@app.get("/api/admin/children")
def api_admin_children(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""儿童列表 JSON 接口。"""
	children = db.query(Child).all()
	out = []
	for c in children:
		out.append({
			"id": c.id,
			"name": c.name,
			"age": c.age,
			"guardian": c.guardian,
			"guardian_phone": c.guardian_phone,
			"case_notes": c.case_notes,
			"stage": c.stage,
			"progress_score": c.progress_score,
			"last_update": c.last_update.isoformat() if c.last_update else None,
		})
	return JSONResponse({"children": out})


@app.post("/children/create")
def create_child(
	name: str = Form(...),
	account: str = Form(None),
	password: str = Form(None),
	age: str = Form(None),
	guardian: str = Form(None),
	guardian_phone: str = Form(None),
	db: Session = Depends(get_db),
	_: bool = Depends(require_admin)
):
	age_value = int(age) if age not in (None, "") else None
	child = Child(
		name=name,
		account=(account or None) or None,
		password=(password or None) or None,
		age=age_value,
		guardian=guardian,
		guardian_phone=guardian_phone,
	)
	db.add(child)
	db.commit()
	return RedirectResponse(url="/children", status_code=303)


@app.post("/api/admin/children")
def api_admin_create_child(
	name: str = Form(...),
	account: str = Form(None),
	password: str = Form(None),
	age: str = Form(None),
	guardian: str = Form(None),
	guardian_phone: str = Form(None),
	db: Session = Depends(get_db),
	_: bool = Depends(require_admin)
):
	age_value = int(age) if age not in (None, "") else None
	child = Child(
		name=name,
		account=(account or None) or None,
		password=(password or None) or None,
		age=age_value,
		guardian=guardian,
		guardian_phone=guardian_phone,
	)
	db.add(child)
	db.commit()
	return JSONResponse({
		"id": child.id,
		"name": child.name,
		"age": child.age,
		"guardian": child.guardian,
		"guardian_phone": child.guardian_phone,
		"case_notes": child.case_notes,
		"stage": child.stage,
		"progress_score": child.progress_score,
		"last_update": child.last_update.isoformat() if child.last_update else None,
	})


@app.post("/children/delete")
def delete_child(child_id: int = Form(...), db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	try:
		db.query(Interaction).filter_by(child_id=child_id).delete()
		db.query(Conversation).filter_by(child_id=child_id).delete()
		db.delete(child)
		db.commit()
	except Exception as e:
		db.rollback()
		raise HTTPException(status_code=500, detail=f"删除失败: {e}")
	return RedirectResponse(url="/children", status_code=303)


@app.get("/children/{child_id}", response_class=HTMLResponse)
def view_child(request: Request, child_id: int, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	interactions = db.query(Interaction).filter_by(child_id=child_id).order_by(Interaction.timestamp).all()
	interactions_serialized = []
	for it in interactions:
		interactions_serialized.append({
			"id": it.id,
			"user_input": it.user_input,
			"bot_response": it.bot_response,
			"timestamp": it.timestamp.isoformat() if it.timestamp else None,
			"conversation_id": it.conversation_id,
		})
	return templates.TemplateResponse("child_detail.html", {"request": request, "child": child, "interactions": interactions_serialized})


@app.get("/api/admin/children/{child_id}")
def api_admin_child_detail(child_id: int, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""单个儿童档案 JSON（含基本信息与交互简表）。"""
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	interactions = db.query(Interaction).filter_by(child_id=child_id).order_by(Interaction.timestamp).all()
	items = []
	for it in interactions:
		items.append({
			"id": it.id,
			"user_input": it.user_input,
			"bot_response": it.bot_response,
			"timestamp": it.timestamp.isoformat() if it.timestamp else None,
			"conversation_id": it.conversation_id,
		})
	return JSONResponse({
		"child": {
			"id": child.id,
			"name": child.name,
			"age": child.age,
			"guardian": child.guardian,
			"guardian_phone": child.guardian_phone,
			"case_notes": child.case_notes,
			"stage": child.stage,
			"progress_score": child.progress_score,
			"last_update": child.last_update.isoformat() if child.last_update else None,
		},
		"interactions": items,
	})


@app.get("/children/{child_id}/edit", response_class=HTMLResponse)
def edit_child_page(request: Request, child_id: int, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	return templates.TemplateResponse("edit_child.html", {"request": request, "child": child})


@app.post("/children/{child_id}/update")
def update_child(
	child_id: int,
	name: str = Form(...),
	account: str = Form(None),
	password: str = Form(None),
	age: str = Form(None),
	guardian: str = Form(None),
	guardian_phone: str = Form(None),
	stage: str = Form(None),
	case_notes: str = Form(None),
	db: Session = Depends(get_db),
	_: bool = Depends(require_admin)
):
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	age_value = int(age) if age not in (None, "") else None
	child.name = name
	# 账号可修改，密码仅在填写时更新
	child.account = (account or None) or child.account
	if password not in (None, ""):
		child.password = password
	child.age = age_value
	child.guardian = guardian
	child.guardian_phone = guardian_phone
	child.stage = stage or child.stage
	child.case_notes = case_notes if case_notes is not None else child.case_notes
	child.last_update = datetime.now(timezone.utc)
	db.commit()
	return RedirectResponse(url="/children", status_code=303)


@app.post("/api/admin/children/{child_id}")
def api_admin_update_child(
	child_id: int,
	name: str = Form(...),
	account: str = Form(None),
	password: str = Form(None),
	age: str = Form(None),
	guardian: str = Form(None),
	guardian_phone: str = Form(None),
	stage: str = Form(None),
	case_notes: str = Form(None),
	db: Session = Depends(get_db),
	_: bool = Depends(require_admin)
):
	child = db.query(Child).filter_by(id=child_id).first()
	if not child:
		raise HTTPException(status_code=404, detail="Child not found")
	age_value = int(age) if age not in (None, "") else None
	child.name = name
	child.account = (account or None) or child.account
	if password not in (None, ""):
		child.password = password
	child.age = age_value
	child.guardian = guardian
	child.guardian_phone = guardian_phone
	child.stage = stage or child.stage
	child.case_notes = case_notes if case_notes is not None else child.case_notes
	child.last_update = datetime.now(timezone.utc)
	db.commit()
	return JSONResponse({
		"id": child.id,
		"name": child.name,
		"age": child.age,
		"guardian": child.guardian,
		"guardian_phone": child.guardian_phone,
		"case_notes": child.case_notes,
		"stage": child.stage,
		"progress_score": child.progress_score,
		"last_update": child.last_update.isoformat() if child.last_update else None,
	})


@app.post("/admin/build_vectorstore")
def build_vectorstore(_: bool = Depends(require_admin)):
	rag = SFBTRAG()
	rag.build_vectorstore()
	return JSONResponse({"status": "built"})


@app.get('/admin/knowledge', response_class=HTMLResponse)
def admin_knowledge(request: Request, db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	items = db.query(SFBTKnowledge).order_by(SFBTKnowledge.last_update.desc()).all()
	return templates.TemplateResponse('knowledge_list.html', {'request': request, 'items': items})


@app.get('/api/admin/knowledge')
def api_admin_knowledge(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	"""知识库条目的 JSON 接口。"""
	items = db.query(SFBTKnowledge).order_by(SFBTKnowledge.last_update.desc()).all()
	out = []
	for k in items:
		out.append({
			"id": k.id,
			"title": k.title,
			"source_url": k.source_url,
			"last_update": k.last_update.isoformat() if k.last_update else None,
		})
	return JSONResponse({"items": out})


@app.get('/chat', response_class=HTMLResponse)
def user_chat(request: Request):
	# 检查是否为访客模式
	if _is_guest(request):
		return templates.TemplateResponse('user_chat.html', {
			'request': request,
			'name': GUEST_NAME,
			'guest_mode': True,
			'intro': SFBTDialogueManager.get_intro_text()
		})
	else:
		# 非访客模式下，检查用户是否已登录
		account = request.cookies.get(USER_COOKIE_NAME)
		if not account:
			# 如果未登录，返回访客模式
			return templates.TemplateResponse('user_chat.html', {
				'request': request,
				'name': GUEST_NAME,
			'guest_mode': True,
			'intro': SFBTDialogueManager.get_intro_text()
			})
		
		# 已登录用户
		from .db_models import SessionLocal, Child
		db = SessionLocal()
		try:
			child = db.query(Child).filter_by(account=account).first()
			if child:
				return templates.TemplateResponse('user_chat.html', {
					'request': request,
					'name': child.name,
					'guest_mode': False,
					'intro': None
				})
			else:
				# 用户关联的儿童不存在，返回访客模式
				return templates.TemplateResponse('user_chat.html', {
					'request': request,
					'name': GUEST_NAME,
					'guest_mode': True,
					'intro': SFBTDialogueManager.get_intro_text()
				})
		finally:
			db.close()


@app.post('/api/web_search')
def api_web_search(query: str = Form(...), top_k: int = Form(3)):
	rag = SFBTRAG()
	try:
		results = rag.web_retrieve(query, top_k=top_k, search_pages=1)
	except Exception as e:
		return JSONResponse({'error': str(e)}, status_code=500)
	out = []
	for r in results:
		snippet = (r.get('content') or '')[:500]
		out.append({'url': r.get('url'), 'title': r.get('title'), 'snippet': snippet})
	return JSONResponse({'results': out})


@app.get('/admin/upload_knowledge', response_class=HTMLResponse)
def upload_knowledge_page(request: Request, _: bool = Depends(require_admin)):
	return templates.TemplateResponse('upload_knowledge.html', {'request': request})


@app.post('/admin/upload_knowledge')
async def upload_knowledge(file: UploadFile = File(...), title: str = Form(None), db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	await _save_uploaded_knowledge(file, title, db)
	try:
		rag = SFBTRAG()
		rag.build_vectorstore()
	except Exception:
		pass
	return RedirectResponse(url='/admin/knowledge', status_code=303)


@app.post('/api/admin/upload_knowledge')
async def api_admin_upload_knowledge(file: UploadFile = File(...), title: str = Form(None), db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	k = await _save_uploaded_knowledge(file, title, db)
	try:
		rag = SFBTRAG()
		rag.build_vectorstore()
	except Exception:
		pass
	return JSONResponse({
		"id": k.id,
		"title": k.title,
		"source_url": k.source_url,
		"last_update": k.last_update.isoformat() if k.last_update else None,
	})


@app.post('/api/admin/knowledge/sync')
def api_admin_knowledge_sync(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	result = _sync_uploads_knowledge(db)
	if result.get("added") or result.get("removed"):
		try:
			rag = SFBTRAG()
			rag.build_vectorstore()
		except Exception:
			pass
	return JSONResponse(result)


@app.post('/admin/knowledge/delete')
def admin_knowledge_delete(knowledge_id: int = Form(...), db: Session = Depends(get_db), _: bool = Depends(require_admin)):
	k = db.query(SFBTKnowledge).filter_by(id=knowledge_id).first()
	if not k:
		return JSONResponse({'error': 'not found'}, status_code=404)
	try:
		if k.source_url and os.path.exists(k.source_url):
			os.remove(k.source_url)
	except Exception:
		pass
	db.delete(k)
	db.commit()
	try:
		rag = SFBTRAG()
		rag.build_vectorstore()
	except Exception:
		pass
	return RedirectResponse(url='/admin/knowledge', status_code=303)


@app.post("/api/chat")
def api_chat(
	request: Request,
	user_input: str = Form(...),
	conversation_id: int = Form(None),
	include_explanation: bool = Form(False),
	enable_web_retrieval: bool = Form(None)
):
	"""非流式聊天接口。
	从 USER_COOKIE_NAME 读取账号；若找得到对应儿童，则使用其 name
	"""
	dm = SFBTDialogueManager()
	if _is_guest(request):
		name = GUEST_NAME
		try:
			result = dm.generate_reply(
				name,
				user_input,
				conversation_id=None,
				include_explanation=include_explanation,
				enable_web_retrieval=enable_web_retrieval,
				persist=False,
			)
		except TypeError as exc:
			if "include_explanation" in str(exc) or "persist" in str(exc):
				result = dm.generate_reply(name, user_input, conversation_id=None, enable_web_retrieval=enable_web_retrieval, persist=False)
			else:
				raise
		if isinstance(result, str):
			return JSONResponse({"error": result}, status_code=500)
		out = {"reply": result.get("reply"), "conversation_id": None}
		if result.get("explanation"):
			out["explanation"] = result.get("explanation")
		return JSONResponse(out)
	account = request.cookies.get(USER_COOKIE_NAME)
	if not account:
		return JSONResponse({"error": "未登录"}, status_code=401)
	with SessionLocal() as db:
		child = db.query(Child).filter(Child.account == account).first()
		if not child or not child.name:
			return JSONResponse({"error": "账号无效"}, status_code=401)
		name = child.name
	try:
		result = dm.generate_reply(
			name,
			user_input,
			conversation_id=conversation_id,
			include_explanation=include_explanation,
			enable_web_retrieval=enable_web_retrieval,
		)
	except TypeError as exc:
		if "include_explanation" in str(exc):
			result = dm.generate_reply(name, user_input, conversation_id=conversation_id, enable_web_retrieval=enable_web_retrieval)
		else:
			raise
	if isinstance(result, str):
		return JSONResponse({"error": result}, status_code=500)
	out = {"reply": result.get("reply"), "conversation_id": result.get("conversation_id")}
	if result.get("explanation"):
		out["explanation"] = result.get("explanation")
	return JSONResponse(out)


def _format_sse(event: str, data: str) -> bytes:
	msg = f"event: {event}\n"
	for line in str(data).splitlines():
		msg += f"data: {line}\n"
	msg += "\n"
	return msg.encode('utf-8')


@app.get('/admin/alerts/stream')
def admin_alerts_stream(_: bool = Depends(require_admin)):
	q = alert_subscribe()
	def event_stream():
		last_ping = time.time()
		try:
			while True:
				try:
					item = q.get(timeout=10)
					yield _format_sse('alert', json.dumps(item, ensure_ascii=False))
				except queue.Empty:
					pass
				if time.time() - last_ping > 30:
					yield _format_sse('ping', '{}')
					last_ping = time.time()
		finally:
			alert_unsubscribe(q)
	return StreamingResponse(event_stream(), media_type='text/event-stream')


@app.post('/api/chat/stream')
def api_chat_stream(
	request: Request,
	child_name: str = Form(None),
	user_input: str = Form(...),
	conversation_id: int = Form(None),
	include_explanation: bool = Form(False),
	enable_web_retrieval: bool = Form(None)
):
	dm = SFBTDialogueManager()
	q = queue.Queue()
	sentinel = object()
	def progress_cb(msg):
		try:
			q.put({'type': 'progress', 'msg': msg})
		except Exception:
			pass
	
	if _is_guest(request):
		name = GUEST_NAME
	else:
		account = request.cookies.get(USER_COOKIE_NAME)
		if not account:
			return JSONResponse({"error": "未登录"}, status_code=401)
		with SessionLocal() as db:
			child = db.query(Child).filter(Child.account == account).first()
			if not child or not child.name:
				return JSONResponse({"error": "账号无效"}, status_code=401)
			name = child.name

	def worker():
		try:
			try:
				result = dm.generate_reply(
					name,
					user_input,
					conversation_id=conversation_id,
					include_explanation=include_explanation,
					progress_callback=progress_cb,
					enable_web_retrieval=enable_web_retrieval,
					persist=not _is_guest(request),
				)
			except TypeError as exc:
				if "include_explanation" in str(exc) or "progress_callback" in str(exc) or "persist" in str(exc):
					result = dm.generate_reply(name, user_input, conversation_id=conversation_id, enable_web_retrieval=enable_web_retrieval, persist=not _is_guest(request))
				else:
					raise
			q.put({'type': 'result', 'result': result})
		except Exception as e:
			q.put({'type': 'error', 'error': str(e)})
		finally:
			q.put(sentinel)
	t = threading.Thread(target=worker, daemon=True)
	t.start()
	def event_stream():
		while True:
			item = q.get()
			if item is sentinel:
				break
			try:
				if item.get('type') == 'progress':
					yield _format_sse('progress', json.dumps({'message': item.get('msg')}, ensure_ascii=False))
				elif item.get('type') == 'result':
					res = item.get('result')
					yield _format_sse('result', json.dumps(res, ensure_ascii=False))
				elif item.get('type') == 'error':
					yield _format_sse('error', json.dumps({'error': item.get('error')}, ensure_ascii=False))
			except Exception:
				try:
					yield _format_sse('progress', json.dumps({'message': str(item)}))
				except Exception:
					pass
		yield _format_sse('done', '{}')
	return StreamingResponse(event_stream(), media_type='text/event-stream')


@app.post("/api/conversations/create")
def api_create_conversation(request: Request, child_name: str = Form(None), title: str = Form(None)):
	"""创建会话。	
	- 从用户登录时设置的 USER_COOKIE_NAME 中读取账号，将其作为“儿童标识”传给对话管理器，
	  从而实现“儿童信息与账号相关联”。
	"""
	dm = SFBTDialogueManager()
	if _is_guest(request):
		return JSONResponse({"conversation_id": None, "intro": dm.get_intro_text()})
	account = request.cookies.get(USER_COOKIE_NAME)
	if not account:
		return JSONResponse({"error": "未登录"}, status_code=401)
	with SessionLocal() as db:
		child = db.query(Child).filter(Child.account == account).first()
		if not child or not child.name:
			return JSONResponse({"error": "账号无效"}, status_code=401)
		name = child.name
	conv_id = dm.create_conversation(name, title=title)
	intro = None
	try:
		intro = dm.generate_intro_message(name, conv_id)
	except Exception:
		intro = None
	return JSONResponse({"conversation_id": conv_id, "intro": intro})


@app.get("/api/conversations/{child_id}")
def api_list_conversations(child_id: int, db: Session = Depends(get_db)):
	convs = db.query(Conversation).filter_by(child_id=child_id).order_by(Conversation.created_at.desc()).all()
	items = []
	for c in convs:
		items.append({"id": c.id, "title": c.title, "created_at": c.created_at.isoformat() if c.created_at else None})
	return JSONResponse({"conversations": items})


@app.get('/api/conversations/by-name')
def api_list_conversations_by_name(child_name: str, db: Session = Depends(get_db)):
	child = db.query(Child).filter_by(name=child_name).first()
	if not child:
		return JSONResponse({"conversations": []})
	convs = db.query(Conversation).filter_by(child_id=child.id).order_by(Conversation.created_at.desc()).all()
	items = []
	for c in convs:
		items.append({"id": c.id, "title": c.title, "created_at": c.created_at.isoformat() if c.created_at else None})
	return JSONResponse({"conversations": items, "child_id": child.id})


@app.get('/api/child/history/by-name')
def api_child_history_by_name(child_name: str, db: Session = Depends(get_db)):
	child = db.query(Child).filter_by(name=child_name).first()
	if not child:
		return JSONResponse({"history": []})
	interactions = db.query(Interaction).filter_by(child_id=child.id).order_by(Interaction.timestamp).all()
	items = []
	for it in interactions:
		items.append({
			"id": it.id,
			"user_input": it.user_input,
			"bot_response": it.bot_response,
			"timestamp": it.timestamp.isoformat() if it.timestamp else None,
			"conversation_id": it.conversation_id,
		})
	return JSONResponse({"history": items, "child_id": child.id})


@app.get('/api/child/{child_id}/history')
def api_child_history(child_id: int, db: Session = Depends(get_db)):
	interactions = db.query(Interaction).filter_by(child_id=child_id).order_by(Interaction.timestamp).all()
	items = []
	for it in interactions:
		items.append({
			"id": it.id,
			"user_input": it.user_input,
			"bot_response": it.bot_response,
			"timestamp": it.timestamp.isoformat() if it.timestamp else None,
			"conversation_id": it.conversation_id,
		})
	return JSONResponse({"history": items})


@app.get("/api/conversations/{conversation_id}/history")
def api_conversation_history(conversation_id: int, db: Session = Depends(get_db)):
	interactions = db.query(Interaction).filter_by(conversation_id=conversation_id).order_by(Interaction.timestamp).all()
	items = []
	for it in interactions:
		items.append({"id": it.id, "user_input": it.user_input, "bot_response": it.bot_response, "timestamp": it.timestamp.isoformat() if it.timestamp else None})
	return JSONResponse({"history": items})


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
	return {"children": db.query(Child).count(), "knowledge": db.query(SFBTKnowledge).count()}


if __name__ == "__main__":
	uvicorn.run("src.app:app", host="127.0.0.1", port=8000, reload=True)