import os
import json
import base64
import time
import threading

import httpx
from loguru import logger

GITHUB_API = "https://api.github.com"
_last_get_sha = {}

_lock = threading.Lock()
_pending_uploads = {}
_has_work = threading.Event()


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
        if data.get("sha"):
            _last_get_sha[path] = data["sha"]
        raw = base64.b64decode(data.get("content", "")).decode()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"remote_state download error: {e}")
        return None


def _sync_upload_state(path, payload):
    """Выполняет реальную выгрузку самой свежей версии файла в GitHub."""
    repo = _repo()
    if not repo:
        return
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    message = f"auto: update {path}"

    for attempt in range(3):
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

        if code in (409, 500, 502, 503):
            time.sleep(2.5 * (attempt + 1))
            continue

        logger.error(f"remote_state upload error: {code} {data}")
        return


def _upload_worker():
    """Берёт только самый свежий срез каждого файла, исключая очередь устаревших коммитов и 409 Conflict."""
    while True:
        _has_work.wait()
        time.sleep(1.0)
        with _lock:
            if not _pending_uploads:
                _has_work.clear()
                continue
            path, payload = next(iter(_pending_uploads.items()))
            del _pending_uploads[path]
            if not _pending_uploads:
                _has_work.clear()
        try:
            _sync_upload_state(path, payload)
        except Exception as e:
            logger.error(f"upload_worker error: {e}")


threading.Thread(target=_upload_worker, daemon=True).start()


def upload_state(path, payload):
    """Сохраняет последний актуальный срез файла для фоновой выгрузки."""
    if os.getenv("GITHUB_TOKEN"):
        with _lock:
            _pending_uploads[path] = payload
            _has_work.set()
