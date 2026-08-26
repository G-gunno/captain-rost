import os
import base64
import time
import threading

import httpx
from loguru import logger

_lock = threading.Lock()
GITHUB_API = "https://api.github.com"
_last_get_sha = {}  # кэш SHA по path: {path: sha}


def _headers():
    token = os.getenv("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}


def _repo():
    return os.getenv("GITHUB_REPO", "").strip()


def _do(method, url, **kwargs):
    try:
        with httpx.Client(timeout=20) as c:
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


def _get_sha(path):
    """Получить актуальный SHA файла из ветки learner-state."""
    repo = _repo()
    code, data = _do("GET", f"{GITHUB_API}/repos/{repo}/contents/{path}",
                     params={"ref": "learner-state"})
    if code == 200:
        sha = data.get("sha")
        _last_get_sha[path] = sha
        return sha, data.get("content", "")
    return None, ""


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
        import json
        return json.loads(raw)
    except Exception as e:
        logger.error(f"remote_state download error: {e}")
        return None


def upload_state(path, payload):
    """Загрузить JSON в ветку learner-state с retry на 409 Conflict."""
    repo = _repo()
    if not repo or not os.getenv("GITHUB_TOKEN"):
        return

    import json
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    message = f"auto: update {path}"

    # Сериализуем записи по path, чтобы два файла не дрались одновременно
    with _lock:
        for attempt in range(3):
            # Получаем свежий SHA перед каждой попыткой
            sha, old_content = _get_sha(path)

            body = {
                "message": message,
                "content": content,
                "branch": "learner-state",
            }
            if sha:
                body["sha"] = sha

            code, data = _do("PUT", f"{GITHUB_API}/repos/{repo}/contents/{path}", json=body)

            if code in (200, 201):
                # Успех — обновляем кэш SHA
                new_sha = data.get("content", {}).get("sha")
                if new_sha:
                    _last_get_sha[path] = new_sha
                return

            if code == 409:
                # Конфликт версий — ждём и пробуем снова со свежим SHA
                logger.warning(f"remote_state: 409 conflict on {path}, retry {attempt+1}/3")
                time.sleep(0.5)
                continue

            # Другая ошибка — логируем и выходим
            logger.error(f"remote_state upload error: {code} {data}")
            return

        logger.error(f"remote_state: 3 попытки не помогли для {path}")
