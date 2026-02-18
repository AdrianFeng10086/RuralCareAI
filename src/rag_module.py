"""RAG 检索模块（在线 SERP 片段 + 本地向量库）。

提供：
- web_retrieve: 仅基于搜索结果摘要构建临时索引并检索
- build_vectorstore/retrieve: 用数据库知识构建 FAISS 并检索
"""
from langchain_community.vectorstores import FAISS
try:
	from langchain.schema import Document
except Exception:
	try:
		from langchain.docstore.document import Document
	except Exception:
		from dataclasses import dataclass

		@dataclass
		class Document:
			page_content: str
			metadata: dict
from src.db_models import SFBTKnowledge, SessionLocal
from typing import TYPE_CHECKING
import hashlib
import os
import requests
from bs4 import BeautifulSoup
import urllib.parse
import trafilatura
import time
import re

embedding_model = None
HuggingFaceEmbeddings = None


class SimpleHashEmbeddings:
	def __init__(self, dim: int = 256):
		self.dim = dim

	def _embed(self, text: str):
		data = (text or "").ensrc("utf-8")
		digest = hashlib.sha256(data).digest()
		buf = bytearray()
		while len(buf) < self.dim:
			digest = hashlib.sha256(digest).digest()
			buf.extend(digest)
		return [b / 255.0 for b in buf[: self.dim]]

	def embed_documents(self, texts):
		return [self._embed(t) for t in texts]

	def embed_query(self, text):
		return self._embed(text)
try:
	try:
		from langchain_huggingface import HuggingFaceEmbeddings  # type: ignore
	except Exception:
		from langchain_community.embeddings import HuggingFaceEmbeddings  # type: ignore
except Exception:
	HuggingFaceEmbeddings = None


class SFBTRAG:
	def __init__(self):
		self.session = SessionLocal()
		self.vectorstore = None
		self._web_disabled = False
		self._http_headers = {
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
		}

	def _ensure_embedding_model(self):
		global embedding_model, HuggingFaceEmbeddings
		if embedding_model is None:
			if HuggingFaceEmbeddings is None:
				try:
					try:
						from langchain_huggingface import HuggingFaceEmbeddings as _HFE  # type: ignore
					except Exception:
						from langchain_community.embeddings import HuggingFaceEmbeddings as _HFE  # type: ignore
					HuggingFaceEmbeddings = _HFE
				except Exception as e:
					print("⚠️ 无法导入 HuggingFaceEmbeddings（缺少依赖），已切换到简易嵌入实现：", e)
					embedding_model = SimpleHashEmbeddings()
					return True
			try:
				mirror = os.getenv("HF_ENDPOINT") or os.getenv("HF_MIRROR")
				if mirror:
					os.environ["HF_ENDPOINT"] = mirror
				local_only = os.getenv("HF_EMBEDDINGS_LOCAL_ONLY", "1").strip().lower() in ("1", "true", "yes")
				kwargs = {}
				if local_only:
					kwargs["model_kwargs"] = {"local_files_only": True}
				embedding_model = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese", **kwargs)
			except Exception as e:
				print("⚠️ 初始化 HuggingFace Embedding 失败，已切换到简易嵌入实现：", e)
				embedding_model = SimpleHashEmbeddings()
		return True


	def _search_baidu(self, query: str, search_pages: int, timeout: int):
		urls = []
		for p in range(1, max(1, search_pages) + 1):
			q = urllib.parse.quote_plus(query)
			pn = (p - 1) * 10
			url = f"https://www.baidu.com/s?wd={q}&pn={pn}"
			resp = requests.get(url, headers=self._http_headers, timeout=min(timeout, 5))
			if resp.status_src != 200:
				continue
			soup = BeautifulSoup(resp.text, 'html.parser')
			for h3 in soup.find_all('h3'):
				a = h3.find('a', href=True)
				if not a:
					continue
				href = a['href']
				if href.startswith('http'):
					title = a.get_text(strip=True)
					snippet = ''
					try:
						parent = h3.parent
						if parent:
							ptag = parent.find('div') or parent.find('p')
							if ptag:
								snippet = ptag.get_text(" ", strip=True)[:180]
					except Exception:
						snippet = ''
					urls.append({"url": href, "title": title, "snippet": snippet})
			if not urls:
				for a in soup.select('a[href^="http"]'):
					urls.append({"url": a['href'], "title": a.get_text(strip=True), "snippet": ""})
			time.sleep(0.5)
		return urls

	def _search_bing(self, query: str, search_pages: int, timeout: int):
		items = []
		for p in range(1, max(1, search_pages) + 1):
			q = urllib.parse.quote_plus(query)
			first = 1 + (p - 1) * 10
			url = f"https://cn.bing.com/search?q={q}&first={first}"
			resp = requests.get(url, headers=self._http_headers, timeout=min(timeout, 5))
			if resp.status_src != 200:
				continue
			soup = BeautifulSoup(resp.text, 'html.parser')
			for li in soup.select('li.b_algo'):
				a = li.select_one('h2 > a')
				if not a or not a.get('href'):
					continue
				href = a.get('href')
				title = a.get_text(strip=True)
				cap = li.select_one('div.b_caption p') or li.select_one('p')
				snippet = cap.get_text(" ", strip=True)[:180] if cap else ''
				items.append({"url": href, "title": title, "snippet": snippet})
			time.sleep(0.5)
		return items

	def web_retrieve(self, query: str, top_k: int = 3, search_pages: int = 1, timeout: int = 8, progress_callback=None, prefer_baidu: bool = False):
		if self._web_disabled:
			if callable(progress_callback):
				try:
					progress_callback("在线检索此前超时，本次已直接跳过（使用本地知识继续回答）。")
				except Exception:
					pass
			return []

		urls = []
		provider_used = None
		provider_errors = []
		if callable(progress_callback):
			try:
				progress_callback("开始搜索相关网页...")
			except Exception:
				pass
		provider_pairs = [("Bing", self._search_bing), ("Baidu", self._search_baidu)]
		if prefer_baidu:
			provider_pairs.sort(key=lambda item: 0 if item[0] == "Baidu" else 1)
		for name, func in provider_pairs:
			try:
				if callable(progress_callback):
					try:
						progress_callback(f"正在尝试 {name} 搜索...")
					except Exception:
						pass
				urls = func(query, search_pages, timeout)
				if urls:
					provider_used = name
					break
			except requests.RequestException as exc:
				provider_errors.append(f"{name}: {exc}")
			except Exception as exc:
				provider_errors.append(f"{name}: {exc}")
		if not urls:
			self._web_disabled = True
			if callable(progress_callback):
				try:
					err_msg = provider_errors[-1] if provider_errors else "未获取到搜索结果"
					progress_callback(f"在线搜索失败（{err_msg}），已跳过在线检索。")
				except Exception:
					pass
			return []
		self._web_disabled = False
		if callable(progress_callback):
			try:
				progress_callback(f"{provider_used} 共找到 {len(urls)} 条候选结果，准备生成检索片段...")
			except Exception:
				pass
		seen = set()
		unique_urls = []
		for entry in urls:
			url_key = entry.get('url') if isinstance(entry, dict) else str(entry)
			if not url_key or url_key in seen:
				continue
			seen.add(url_key)
			unique_urls.append(entry)
			if len(unique_urls) >= 20:
				break
		cn_only = str(os.getenv("CN_ONLY", "0")).strip().lower() in ("1", "true", "yes")
		filter_applied = False
		filtered_out = 0
		if cn_only and unique_urls:
			def _is_cn_accessible(u: str) -> bool:
				try:
					parsed = urllib.parse.urlparse(u)
					host = (parsed.netloc or '').split(':')[0].lower()
					if host.startswith('www.'):
						host = host[4:]
					if host.endswith('.cn'):
						return True
					allowed_roots = ("baidu.com", "bing.com", "cn.bing.com","zhihu.com")
					for root in allowed_roots:
						if host == root or host.endswith('.' + root):
							return True
					return False
				except Exception:
					return False
			kept = []
			for item in unique_urls:
				u = item.get('url') if isinstance(item, dict) else str(item)
				if _is_cn_accessible(u):
					kept.append(item)
			filtered_out = max(0, len(unique_urls) - len(kept))
			unique_urls = kept
			filter_applied = True
			if callable(progress_callback):
				try:
					progress_callback(f"已启用 CN_ONLY 过滤：剔除 {filtered_out} 条非国内可访问域，保留 {len(unique_urls)} 条。")
				except Exception:
					pass
			if not unique_urls:
				if callable(progress_callback):
					try:
						progress_callback("过滤后没有可用的候选结果，已跳过在线检索。")
					except Exception:
						pass
				return []
		relevance_filter = str(os.getenv("WEB_RELEVANCE_FILTER", "1")).strip().lower() in ("1", "true", "yes")
		irrelevant_filtered = 0
		keywords_used: list[str] = []
		if relevance_filter and unique_urls:
			def _extract_keywords(q: str):
				toks = []
				buf = []
				for ch in q:
					if '\u4e00' <= ch <= '\u9fff':
						buf.append(ch)
					else:
						if len(buf) >= 2:
							toks.append(''.join(buf))
						buf = []
				if len(buf) >= 2:
					toks.append(''.join(buf))
				toks.extend(re.findall(r"[A-Za-z]{3,}", q))
				uniq = []
				for t in toks:
					if t not in uniq:
						uniq.append(t)
				return uniq[:8]
			keywords_used = _extract_keywords(query)
			if keywords_used:
				kept2 = []
				for item in unique_urls:
					title = (item.get('title') or '').lower()
					snippet = (item.get('snippet') or '').lower()
					text = f"{title} {snippet}"
					if any(k.lower() in text for k in keywords_used):
						kept2.append(item)
				irrelevant_filtered = max(0, len(unique_urls) - len(kept2))
				if kept2:
					unique_urls = kept2
					if callable(progress_callback):
						try:
							progress_callback(f"相关性过滤：剔除 {irrelevant_filtered} 条与查询关键词不匹配的结果，保留 {len(unique_urls)} 条。")
						except Exception:
							pass
				else:
					if callable(progress_callback):
						try:
							progress_callback("相关性过滤后无匹配，已跳过在线检索候选。")
						except Exception:
							pass
					unique_urls = []
		serp_only = str(os.getenv("SERP_ONLY", "1")).strip().lower() in ("1", "true", "yes")
		docs = []
		if serp_only:
			if callable(progress_callback):
				try:
					progress_callback("仅使用搜索结果摘要（不抓取网页正文）...")
				except Exception:
					pass
			for item in unique_urls:
				title = item.get('title') or ''
				snippet = item.get('snippet') or ''
				url = item.get('url') or ''
				content = (snippet or title or url)
				if not content:
					continue
				docs.append(Document(page_content=content, metadata={"url": url, "title": title}))
		else:
			if callable(progress_callback):
				try:
					progress_callback("开始抓取候选页面并提取文本...")
				except Exception:
					pass
			for item in unique_urls:
				url = item['url'] if isinstance(item, dict) else str(item)
				try:
					r = requests.get(url, headers=self._http_headers, timeout=min(timeout, 5))
				except requests.RequestException as req_err:
					if callable(progress_callback):
						try:
							progress_callback(f"抓取 {url} 失败（{req_err}），已跳过该链接。")
						except Exception:
							pass
					continue
				if r.status_src != 200:
					continue
				text = ''
				try:
					text = trafilatura.extract(r.text) or ''
				except Exception:
					text = ''
				if not text:
					soup = BeautifulSoup(r.text, 'html.parser')
					texts = [t.strip() for t in soup.stripped_strings]
					text = '\n'.join(texts)
				title = ''
				try:
					soup = BeautifulSoup(r.text, 'html.parser')
					if soup.title:
						title = soup.title.string.strip()
				except Exception:
					title = ''
				if text:
					docs.append(Document(page_content=text, metadata={"url": url, "title": title}))
				if len(docs) >= 20:
					break
		if callable(progress_callback):
			try:
				progress_callback(f"已生成 {len(docs)} 个临时检索文档，准备进行向量检索...")
			except Exception:
				pass
		if not docs:
			return []
		if not self._ensure_embedding_model():
			if callable(progress_callback):
				try:
					progress_callback("嵌入模型不可用，已跳过向量检索。")
				except Exception:
					pass
			return []
		try:
			if callable(progress_callback):
				try:
					progress_callback("正在构建临时向量索引并进行相似度检索...")
				except Exception:
					pass
			temp_vs = FAISS.from_documents(docs, embedding_model)
			results = temp_vs.similarity_search(query, k=top_k)
			out = []
			for r in results:
				item = {
					"url": r.metadata.get('url'),
					"title": r.metadata.get('title'),
					"content": r.page_content,
				}
				try:
					item["provider_used"] = provider_used
					item["filter_applied"] = filter_applied
					item["web_filter_count"] = filtered_out
					item["web_filtered_remaining"] = len(unique_urls)
					item["relevance_filter_applied"] = relevance_filter
					item["web_irrelevant_filtered_count"] = irrelevant_filtered
					if keywords_used:
						item["keywords_used"] = keywords_used
				except Exception:
					pass
				out.append(item)
			if callable(progress_callback):
				try:
					progress_callback(f"向量检索完成，返回 {len(out)} 条片段。")
				except Exception:
					pass
			return out
		except Exception as e:
			print('临时向量检索失败：', e)
			return []

	def build_vectorstore(self):
		docs = []
		for row in self.session.query(SFBTKnowledge).all():
			content = f"{row.title}\n{row.content}"
			docs.append(Document(page_content=content, metadata={"type": "knowledge"}))
		if not docs:
			self.vectorstore = None
			print("⚠️ 数据库中没有可用于构建向量库的记录，请先准备语料或知识库数据。")
			return
		global embedding_model, HuggingFaceEmbeddings
		if embedding_model is None:
			if HuggingFaceEmbeddings is None:
				try:
					try:
						from langchain_huggingface import HuggingFaceEmbeddings as _HFE  # type: ignore
					except Exception:
						from langchain_community.embeddings import HuggingFaceEmbeddings as _HFE  # type: ignore
					HuggingFaceEmbeddings = _HFE
				except Exception as e:
					print("⚠️ 无法导入 HuggingFaceEmbeddings，使用简易嵌入构建向量库：", e)
					embedding_model = SimpleHashEmbeddings()
			try:
				if isinstance(embedding_model, SimpleHashEmbeddings):
					pass
				else:
					mirror = os.getenv("HF_ENDPOINT") or os.getenv("HF_MIRROR")
					if mirror:
						os.environ["HF_ENDPOINT"] = mirror
					local_only = os.getenv("HF_EMBEDDINGS_LOCAL_ONLY", "1").strip().lower() in ("1", "true", "yes")
					kwargs = {}
					if local_only:
						kwargs["model_kwargs"] = {"local_files_only": True}
					embedding_model = HuggingFaceEmbeddings(model_name="shibing624/text2vec-base-chinese", **kwargs)
			except Exception as e:
				print("⚠️ 初始化 HuggingFace Embedding 失败，使用简易嵌入构建向量库：", e)
				embedding_model = SimpleHashEmbeddings()
		self.vectorstore = FAISS.from_documents(docs, embedding_model)
		print(f"✅ 向量库构建完成，共 {len(docs)} 条知识文档")

	def retrieve(self, query, top_k=3):
		if not self.vectorstore:
			self.build_vectorstore()
		if not self.vectorstore:
			return ""
		results = self.vectorstore.similarity_search(query, k=top_k)
		if not results:
			return ""
		context = "\n\n".join([r.page_content for r in results])
		return context