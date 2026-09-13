import os
import json
import base64
import time
import threading
import queue

import httpx
from loguru import logger

GITHUB_API = "https://api.github.com"
_last_get_sha = {}

# Очередь задач на выгрузку, чтобы не блокировать Event Loop
_upload_queue = queue.Queue()


def _headers():
    token = os.getenv("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}


def _repo():
    return os.getenv("GITHUB_REPO", "").strip()


def _do(method, url, **kwargs):
    try:
        with httpx.Client(timeout=15) as c:
            r = c.request(method, url, headers=_headers(), **kwargs)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        logger.error(f"remote_state request error: {e}")
        return 0, {}


def ensure_branch():
    """Создаёт ветку learner-state, если её нет."""
    repo = _repo()
    if not repo or not os.getenv("GITHUB_TOKEN"):
        return
    code, main_ref = _do("GET", f"{GITHUB_API}/repos/{repo}/git/ref/heads/main")
    if code != 200:
        logger.error(f"remote_state: не удалось получить main ref: {code}")
        return
    sha = main_ref.get("object", {}).get("sha")
    code, _ = _do("POST", f"{GITHUB_API}/repos/{repo}/git/refs",
                  json={"ref": "refs/heads/learner-state", "sha": sha})
    if code in (201, 422):
        logger.info("remote_state: ветка learner-state готова")


def download_state(path):
    """Скачать JSON-файл из ветки learner-state."""
    repo = _repo()
    if not repo or not os.getenv("GITHUB_TOKEN"):
        return None
    try:
        code, data = _do("GET", f"{GITHUB_API}/repos/{repo}/contents/{path}",
                         params={"ref": "learner-state"})
        if code != 200:
            return None
        raw = base64.b64decode(data.get("content", "")).decode()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"remote_state download error: {e}")
        return None


def _sync_upload_state(path, payload):
    """Выполняет реальную выгрузку в GitHub (Синхронно)"""
    repo = _repo()
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    message = f"auto: update {path}"

    for attempt in range(3):
        # Запрашиваем свежий SHA файла перед апдейтом
        code, data = _do("GET", f"{GITHUB_API}/repos/{repo}/contents/{path}", params={"ref": "learner-state"})
        sha = data.get("sha") if code == 200 else _last_get_sha.get(path)

        body = {"message": message, "content": content, "branch": "learner-state"}
        if sha:
            body["sha"] = sha

        code, data = _do("PUT", f"{GITHUB_API}/repos/{repo}/contents/{path}", json=body)

        if code in (200, 201):
            new_sha = data.get("content", {}).get("sha")
            if new_sha:
                _last_get_sha[path] = new_sha
            return

        if code == 409:
            logger.warning(f"remote_state: 409 conflict on {path}, retry {attempt+1}/3")
            time.sleep(1.5)  # Спим в фоновом потоке, не вешая бота
            continue

        logger.error(f"remote_state upload error: {code} {data}")
        return


def _upload_worker():
    """Разгребает очередь выгрузок строго по одному."""
    while True:
        path, payload = _upload_queue.get()
        try:
            _sync_upload_state(path, payload)
        except Exception as e:
            logger.error(f"upload_worker error: {e}")
        finally:
            _upload_queue.task_done()


# Запускаем один фоновый поток (Daemon) при старте модуля
threading.Thread(target=_upload_worker, daemon=True).start()


def upload_state(path, payload):
    """Отправляет задачу в очередь, моментально возвращая управление Event Loop'у."""
    if os.getenv("GITHUB_TOKEN"):
        _upload_queue.put((path, payload))
