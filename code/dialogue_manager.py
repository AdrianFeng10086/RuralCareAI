import re
import os
import json
import logging
from types import SimpleNamespace
from typing import Optional, List, Dict, Any
import requests 
from .db_models import Base, Child, Conversation, CrisisAlert, Interaction, SessionLocal, engine
Base.metadata.create_all(bind=engine)
from .rag_module import SFBTRAG

class SFBTDialogueManager:
    STAGES = ["目标设定阶段", "例外探索阶段", "量表问题阶段", "奇迹问题阶段", "行动计划阶段"]

    def __init__(self, model_name: str = None):
        self.db = SessionLocal()
        self.rag = SFBTRAG()

        # 环境变量工具
        env = os.environ.get
        to_bool = lambda v, default: str(v).strip().lower() in ('1', 'true', 'yes') if v is not None else default
        to_int = lambda v, default: int(v) if v and str(v).strip().isdigit() and int(v) > 0 else default

        self.enable_web_retrieval = to_bool(env('ENABLE_WEB_RETRIEVAL_DEFAULT', '1'), True)
        self.enable_local_retrieval = True
        self.web_top_k = to_int(env('WEB_RETRIEVAL_TOP_K', '1'), 1)
        self.web_search_pages = to_int(env('WEB_RETRIEVAL_PAGES', '2'), 2)
        self.web_timeout = to_int(env('WEB_RETRIEVAL_TIMEOUT', '10'), 10)
        self.web_prefer_baidu = to_bool(env('WEB_PREFER_BAIDU', '1'), True)
        self.max_context_chars = to_int(env('MAX_CONTEXT_CHARS', '1800'), 1800)
        self.model_name = (model_name or env("API_MODEL")).strip()
        self.temperature = float(env("TEMPERATURE", "0.7"))
        self.context_window = to_int(env("API_NUM_CTX", "2048"), 2048)
        self.max_predict_tokens = to_int(env("API_MAX_TOKENS", "768"), 512)

        # API 配置
        self.api_url = (env("DEEPSEEK-API-URL")).strip()
        self.api_key = (env("DEEPSEEK-API")).strip()
        self.api_timeout = to_int(env("API_TIMEOUT", "30"), 30)

        # 日志（写在 run.py 同级目录）
        self.logger = logging.getLogger("SFBTDialogueManager")
        if not self.logger.handlers:
            try:
                base_dir = os.path.dirname(os.path.abspath(os.path.join(__file__, os.pardir)))
                log_path = os.path.join(base_dir, "sfbt_api_errors.log")
            except Exception:
                log_path = "sfbt_api_errors.log"
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

        # 兜底确保表已创建
        try:
            from code.db_models import Base, engine
            Base.metadata.create_all(bind=engine)
        except Exception as e:
            self._log("DB init failed: %s", e, level=logging.WARNING)

  

    def _log(self, msg, *args, level=logging.INFO):
        try: self.logger.log(level, msg, *args)
        except Exception: pass

    def _get_child(self, name):
        child = self.db.query(Child).filter_by(name=name).first()
        if not child:
            child = Child(name=name)
            self.db.add(child); self.db.commit()
        return child

    @staticmethod
    def get_intro_text() -> str:
        return (
            "我是小益，SFBT咨询师。我会倾听你的需求和感受，用耐心陪伴你一起探索改变的方法。"
            "我们可能会一起尝试一些简单的小活动，找到让你感觉更好的方式。如果你愿意分享你的经历或感受，随时可以告诉我。"
            "让我们一起慢慢成长，找到属于你的小小改变！"
        )

    def _next_stage(self, current_stage):
        if current_stage not in self.STAGES: return self.STAGES[0]
        idx = self.STAGES.index(current_stage)
        return self.STAGES[min(idx + 1, len(self.STAGES) - 1)]

    def _build_chat_options(self, temperature=None):
        opts = {}
        t = temperature if temperature is not None else self.temperature
        if t is not None: opts["temperature"] = t
        if self.context_window: opts["num_ctx"] = self.context_window
        if self.max_predict_tokens: opts["num_predict"] = self.max_predict_tokens
        return opts

    def _call_api(self, messages, temperature=None):
        if not self.api_url:
            raise RuntimeError("API URL not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": self.max_predict_tokens,
        }
        try:
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=self.api_timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self._log("API call failed: %s", e, level=logging.ERROR)
            raise



    def _extract_reply(self, resp) -> str:
        try:
            if hasattr(resp, 'message') and hasattr(resp.message, 'content'):
                return resp.message.content
            if isinstance(resp, dict):
                return (
                    resp.get("content") or
                    resp.get("message", {}).get("content") or
                    resp.get("choices", [{}])[0].get("message", {}).get("content") or
                    resp.get("choices", [{}])[0].get("text") or
                    resp.get("data", {}).get("content") or
                    ""
                )
        except Exception: pass
        return str(resp) if resp else ""

    def _is_valid_reply(self, reply: str) -> bool:
        if not reply or not (text := reply.strip()): return False
        if len(text) < 80: return False
        sentences = [s for s in re.split(r'[。！？!?\.]+', text) if s.strip()]
        if len(sentences) < 2: return False
        if re.search(r"error|exception|failed|无法|抱歉", text, re.I): return False
        return True

    def _sanitize_reply(self, reply: str) -> str:
        if not reply: return reply
        text = str(reply)
        patterns = [
            r'(?si)思考[:：].*?(?:结论[:：]|回答[:：]|$)',
            r'(?si)Thoughts?:.*?(?:Answer[:：]|Conclusion[:：]|$)',
            r'(?si)\[.*?思考.*?\].*?\[/.*?\]',
            r'(?si)\(.*?thinking.*?\)',
        ]
        for p in patterns:
            text = re.sub(p, '', text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _detect_crisis(self, text: str) -> dict:
        if not text: return {"any": False}
        flags = {
            "suicide": bool(re.search(r"自杀|轻生|想死|不想活|结束生命|跳楼|割腕", text)),
            "self_harm": bool(re.search(r"伤害自己|割伤|自残|自伤", text)),
            "abuse": bool(re.search(r"家暴|虐待|性侵|被打|被骂|被威胁", text)),
            "violence": bool(re.search(r"杀人|报复|爆炸|炸弹|放火|毒|砍|伤害别人", text)),
        }
        flags["any"] = any(flags.values())
        return flags

    def _build_ethics_block(self, crisis: dict = None) -> str:
        line = ""
        if crisis and crisis.get("any"):
            kinds = []
            if crisis.get("suicide"): kinds.append("自杀/轻生")
            if crisis.get("self_harm"): kinds.append("自伤")
            if crisis.get("abuse"): kinds.append("家暴/受虐")
            if crisis.get("violence"): kinds.append("伤害他人")
            tag = "、".join(kinds) or "危机"
            line = f"【系统提醒：当前对话包含{tag}风险，回复时需优先关注安全与求助信息。】\n"

        ethics = (
            "【安全守则（不要直接照搬原文，只需在回复中体现关心和建议）】\n"
            "1. 先表达你在乎ta、担心ta现在的状况，肯定ta愿意说出来的勇气。\n"
            "2. 温柔提醒：安全最重要，如果感到非常危险，要尽快联系信任的大人（如父母中安全的一方、亲戚、老师、学校心理老师），或拨打 110/120。\n"
            "3. 可以建议孩子拨打心理热线：12355 青少年热线 或 800-810-1117（免费），但不要强迫，只是提供选项。\n"
            "4. 在给出安全和求助建议之后，再用 SFBT 的方式问一个小小的、可回答的问题，帮助ta看到哪怕一点点的可能性。\n"
        )
        return line + ethics

    def create_conversation(self, child_name: str, title: str = None) -> int:
        child = self._get_child(child_name)
        title = (title or '').strip() or f"对话{self.db.query(Conversation).filter_by(child_id=child.id).count() + 1}"
        conv = Conversation(child_id=child.id, title=title)
        self.db.add(conv); self.db.commit()
        return conv.id

    def list_conversations(self, child_name: str) -> List[Conversation]:
        child = self._get_child(child_name)
        return self.db.query(Conversation).filter_by(child_id=child.id).order_by(Conversation.created_at.desc()).all()

    def get_conversation_history(self, conversation_id: int) -> List[Interaction]:
        return self.db.query(Interaction).filter_by(conversation_id=conversation_id).order_by(Interaction.timestamp).all()

    def _get_sfbt_prompt(self, child, user_input, interactions, context="", crisis_ethics=""):
        stage = child.stage or "目标设定阶段"
        history = "\n".join(
            f"孩子: {i.user_input}\n小益: {i.bot_response}"
            for i in interactions[-6:]
        ) if interactions else ""

        base = f"""
    {crisis_ethics}
    你是一个严格遵循 SFBT 流程的咨询师“小益”，使用解决导向短期治疗（SFBT）的方式，像一位耐心、温柔的大朋友一样陪伴孩子。

    你的回复必须遵循以下要求：
    1. 只输出一段连续的、口语化的中文，对孩子说话，不要使用列表、编号或 JSON，不要出现 empathy/affirm/question/hope 等英文单词或字段名。
    2. 在这一段话中，自然地包含以下要素，但不要分段标记、不要做「总结」或「说教」：
       - 共情：先用温柔、贴近生活的语言理解和照顾孩子现在的感受；
       - 资源/肯定（可选）：如果合适，可以轻轻点出孩子已经做得不错、或者已经在努力的地方；
       - SFBT 引导问题：用好奇、开放、具体的问题，引导孩子去想「例外时刻」「一点点改变」「量表分数」「下一小步行动」等，而不是讲一大堆道理；
       - 希望/陪伴结尾（可选）：用一句简单的陪伴或鼓励结尾，比如“可以一点点来”“我会陪着你慢慢想”等。
    3. 语气要温柔、真诚、像聊天，不像老师讲课或家长批评，句子不要太长，适合小学生或初中生理解。
    4. 不要说明你在使用什么技术，也不要解释你要做什么，只专注于对孩子的回应。
    5. 下方的“最近几轮对话”“背景信息”等内容只是给你参考，帮助你理解情境，是系统提供给你的内部说明，不要在回复里提到“检索”“搜索结果”“背景信息”“上面这段文字”等，也不要照搬原文句子或网址、标题。

    【如果上面的系统提醒中出现了“风险”字样，说明孩子的内容里可能涉及自杀/自伤/家暴/暴力等严重问题。此时，你在回复时需要特别注意：】
    1. 一定要先表达你在乎和担心孩子现在的状况，肯定ta愿意说出来的勇气，让ta知道“你不是一个人，我在乎你”。
    2. 接着要温柔、清楚地提醒：安全比什么都重要。可以简单提到一两种可选择的求助方式，比如：联系一个稍微信任的大人（如亲戚、老师、学校心理老师），或者在非常危险的时候可以考虑拨打 110/120，心理热线 12355 也可以尝试，但要用“可以”“如果你愿意”这样的说法，不要命令。
    3. 在提到安全和求助之后，再用 SFBT 的方式问一个小小的、现实可行的问题，问题最好和“让自己稍微安全一点/好受一点/多一点支持”有关，比如“现在有没有一个你觉得稍微可靠一点的人，可以先跟ta说一句你现在不太好受？”等，而不是直接跳到“出去玩、做喜欢的事”。
    4. 整个回复依然只是一段自然的对话，不要把这些规则说出来，只把关心、安全提示和小小的问题自然地融进话语里。

    【系统内部信息，仅供你理解，不要说给孩子听】
    当前 SFBT 阶段：{stage}
    最近几轮对话（从早到晚）：
    {history}
    孩子最新说：{user_input}
    相关背景与参考信息：
    {context}
    【系统内部信息结束】

    现在，请根据以上信息，直接给出你对孩子的一段回复。
    """

        if not interactions:
            return base + "\n这是第一次对话 → 在回复中要温柔地引导一个“奇迹问题”，例如“如果今晚有个小小的奇迹发生，明天醒来你会发现哪一件事情有一点点不一样？”，但不要直接说“奇迹问题”这个词。"

        last_user = user_input.strip()
        last_bot = interactions[-1].bot_response if interactions else ""

        if any(kw in last_bot for kw in ["奇迹", "不一样", "理想"]):
            if not any("分" in i.user_input for i in interactions):
                return base + "\n孩子描述了理想状态 → 在回复中温柔地引导一个 0–10 量表问题，例如“如果 0 分代表一点都没有发生、10 分代表已经完全实现了，你觉得现在大概在几分？”"

        if re.search(r"\d+", last_user) and len(last_user) <= 10:
            score = re.search(r"\d+", last_user).group()
            return base + f"\n孩子说在 {score} 分 → 在回复中要肯定孩子已经做到的部分，并引导一个例外/资源问题，例如“你是怎么做到现在有这 {score} 分的？过程中有哪些事情、哪些人或者你自己的哪些努力帮到了你？”"

        if any(kw in last_user.lower() for kw in ["做", "想", "有人", "听", "吃", "走"]):
            if "下一步" not in last_bot:
                return base + "\n孩子提到了一些资源或行动的可能 → 在回复中适当肯定这些资源，并引导一个“下一小步”的问题，例如“如果想再往上走一点点，你觉得可以先从哪一件很小、很可行的事情开始试一试？”"

        if any(kw in last_user for kw in ["问", "看", "写", "聊"]):
            if "什么时候" not in last_bot:
                return base + "\n孩子已经有了一些行动计划 → 在回复中帮助孩子具体化计划，比如“你觉得大概什么时候、在哪里、和谁一起做这件事最合适？你打算怎么开始？”"

        if any(kw in last_user for kw in ["开心", "轻松", "没那么"]):
            return base + "\n孩子感觉比之前好了一些 → 在回复中先肯定这种变化，然后引导一个“稳定与扩散”的问题，例如“你觉得是什么让这种轻松/没那么难受的感觉出现的？我们可以怎么做，让这种感觉多停留一会儿，或者慢慢多一点？”"

        return base

    def generate_reply(self, child_name, user_input, conversation_id: int = None, include_explanation: bool = False, progress_callback=None, enable_web_retrieval: bool = None, persist: bool = True) -> Dict[str, Any]:
        if persist:
            child = self._get_child(child_name)
        else:
            child = SimpleNamespace(name=child_name, stage="目标设定阶段", id=None)
        user_input = user_input.strip()
        web_contexts, web_sources = [], []
        web_query = user_input if (enable_web_retrieval is None or enable_web_retrieval) and self.enable_web_retrieval else ""
        web_retrieval_attempted = False

        # 在线爬虫检索
        if web_query:
            if progress_callback: progress_callback(f"正在搜索：{web_query[:36]}...")
            try:
                if hasattr(self.rag, 'web_retrieve'):
                    web_retrieval_attempted = True
                    results = self.rag.web_retrieve(
                        query=user_input, top_k=self.web_top_k, search_pages=self.web_search_pages,
                        timeout=self.web_timeout, progress_callback=progress_callback, prefer_baidu=self.web_prefer_baidu
                    )
                    for r in results:
                        c = (r.get('content') or r.get('page_content') or '').strip().replace('\r', ' ')[:800]
                        if c: web_contexts.append(c)
                        web_sources.append({
                            "title": r.get('title') or r.get('url') or "来源",
                            "url": r.get('url') or "",
                            "snippet": c[:180]
                        })
            except Exception as e:
                self._log("Web error: %s", e, level=logging.WARNING)
            finally:
                if progress_callback: progress_callback("搜索完成")

        # 本地检索
        local_ctx = ""
        if self.enable_local_retrieval:
            try:
                local_ctx = self.rag.retrieve(user_input).strip().replace('\r', ' ')[:1200]
            except: pass

        context = "\n\n".join(web_contexts + ([local_ctx] if local_ctx else []))
        if self.max_context_chars and context:
            context = context[:self.max_context_chars]

        # 模拟模式
        if str(os.getenv('MOCK_LLM', '')).lower() in ('1', 'true', 'yes'):
            return {
                "reply": "我们来聊聊你希望有什么不一样？",
                "conversation_id": conversation_id or self.create_conversation(child_name),
                "web_sources": web_sources, "web_source_count": len(web_sources)
            }

        # 会话 & 历史
        if persist and not conversation_id:
            conversation_id = self.create_conversation(child_name)

        if persist:
            interactions = self.db.query(Interaction)\
                .filter_by(conversation_id=conversation_id)\
                .order_by(Interaction.timestamp).all()
        else:
            interactions = []

        # 危机 & 伦理
        crisis = self._detect_crisis(user_input)
        ethics = self._build_ethics_block(crisis if crisis.get("any") else None)

        # 动态 SFBT Prompt
        system_prompt = self._get_sfbt_prompt(child, user_input, interactions, context, ethics)

        messages = [{"role": "system", "content": system_prompt}]
        for i in interactions:
            if i.user_input: messages.append({"role": "user", "content": i.user_input})
            if i.bot_response: messages.append({"role": "assistant", "content": i.bot_response})
        messages.append({"role": "user", "content": user_input})

        # 生成
        reply = ""
        for attempt in range(2):
            temp = max(0.3, self.temperature - 0.2 * attempt)
            if progress_callback: progress_callback(f"生成中（{attempt+1}/2）...")
            try:
                resp = self._call_api(messages, temperature=temp)
                raw = self._extract_reply(resp)
                candidate = self._sanitize_reply(raw)
                if self._is_valid_reply(candidate):
                    reply = candidate
                    break
            except Exception as e:
                self._log("Gen failed: %s", e, level=logging.ERROR)

        if not reply:
            # 危机场景下的兜底话术要更明确地关注安全与求助
            if crisis.get("any"):
                reply = (
                    "我听到你现在正经历着很难受、很不安全的事情，我真的很在乎你。"
                    "先要保证你现在是尽量安全的：如果此刻真的很危险，可以尽快联系一个你稍微信任一点的大人，"
                    "比如亲戚、老师、学校的心理老师，或者拨打 110/120，心理热线 12355 也可以先试着打一下。"
                    "在保证安全的前提下，我们也可以一点点想一想：此刻有没有哪一个人、哪一个地方，能让你觉得哪怕只安全一点点、好受一点点？"
                )
            else:
                reply = "我在这里陪着你。你愿意多说一点吗？"

        # 保存
        if persist:
            interaction = Interaction(child_id=child.id, conversation_id=conversation_id,
                                    user_input=user_input, bot_response=reply)
            self.db.add(interaction)
            child.stage = self._next_stage(child.stage)
            self.db.commit()

            # 危机告警
            if crisis.get("any"):
                try:
                    kinds = [v for k, v in {"suicide": "自杀", "self_harm": "自伤", "abuse": "受虐", "violence": "暴力"}.items() if crisis.get(k)]
                    alert = CrisisAlert(child_id=child.id, interaction_id=interaction.id,
                                        flags=crisis, summary="、".join(kinds) + "风险")
                    self.db.add(alert); self.db.commit()
                except Exception as e:
                    self._log("Alert failed: %s", e, level=logging.WARNING)

        if progress_callback: progress_callback("完成")

        result = {
            "reply": reply,
            "conversation_id": conversation_id,
            "web_sources": web_sources,
            "web_source_count": len(web_sources)
        }
        if web_query and web_retrieval_attempted:
            result["web_query"] = web_query
        return result

    def generate_intro_message(self, child_name: str, conversation_id: int) -> Optional[str]:
        if self.db.query(Interaction).filter_by(conversation_id=conversation_id).first():
            return None
        intro = self.get_intro_text()

        interaction = Interaction(
            child_id=self._get_child(child_name).id,
            conversation_id=conversation_id,
            user_input="（系统）开启对话",
            bot_response=intro
        )
        self.db.add(interaction); self.db.commit()
        return intro