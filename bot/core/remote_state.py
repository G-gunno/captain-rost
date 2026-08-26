import os
import json
import base64
import threading

import httpx
from loguru import logger

API = "https://api.github.com"
BRANCH = "learner-state"   # Render деплоит только main, поэтому коммиты сюда не вызывают редеплой


def _token():
    return os.getenv("GITHUB_TOKEN", "").strip()


def _repo():
    return os.getenv("GITHUB_REPO", "").strip()


def _enabled():
    return bool(_token() and _repo())


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"}


def ensure_branch():
    """Создаёт ветку learner-state, если её нет."""
    if not _enabled():
        return
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"{API}/repos/{_repo()}/git/refs/heads/{BRANCH}", headers=_headers())
            if r.status_code == 200:
                return
            m = c.get(f"{API}/repos/{_repo()}/git/refs/heads/main", headers=_headers())
            sha = m.json()["object"]["sha"]
            c.post(f"{API}/repos/{_repo()}/git/refs", headers=_headers(),
                   json={"ref": f"refs/heads/{BRANCH}", "sha": sha})
            logger.info("remote_state: ветка learner-state создана")
    except Exception as e:
        logger.error(f"remote_state ensure_branch error: {e}")


def download_state(path):
    if not _enabled():
        return None
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"{API}/repos/{_repo()}/contents/{path}",
                      headers=_headers(), params={"ref": BRANCH})
            if r.status_code != 200:
                return None
            return json.loads(base64.b64decode(r.json()["content"]))
    except Exception as e:
        logger.error(f"remote_state download error: {e}")
        return None


def upload_state(path, payload):
    if not _enabled():
        return

    def _do():
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(f"{API}/repos/{_repo()}/contents/{path}",
                          headers=_headers(), params={"ref": BRANCH})
                sha = r.json().get("sha") if r.status_code == 200 else None
                body = {
                    "message": "learner: сохранить опыт",
                    "content": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode(),
                    "branch": BRANCH,
                }
                if sha:
                    body["sha"] = sha
                r2 = c.put(f"{API}/repos/{_repo()}/contents/{path}", headers=_headers(), json=body)
                if r2.status_code not in (200, 201):
                    logger.error(f"remote_state upload error: {r2.status_code} {r2.text[:200]}")
        except Exception as e:
            logger.error(f"remote_state upload error: {e}")

    threading.Thread(target=_do, daemon=True).start()
