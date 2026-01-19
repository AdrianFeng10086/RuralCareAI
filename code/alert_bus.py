# Simple in-process alert pub/sub bus for SSE broadcasting (under code package)
import queue
from typing import List, Dict, Any

_subscribers: List[queue.Queue] = []


def subscribe() -> queue.Queue:
	q: queue.Queue = queue.Queue()
	_subscribers.append(q)
	return q


def unsubscribe(q: queue.Queue) -> None:
	try:
		_subscribers.remove(q)
	except ValueError:
		pass


def publish(alert: Dict[str, Any]) -> None:
	for q in list(_subscribers):
		try:
			q.put(alert, block=False)
		except Exception:
			pass