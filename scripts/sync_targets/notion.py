from __future__ import annotations

import copy
import logging
import time
from typing import Any, Callable, Mapping, Optional

import requests

NOTION_VERSION = "2022-06-28"
OWNERSHIP_MARKER = "Managed by GithubStarsIndex Notion Sync. Exclusive database."

DEFAULT_DATABASE_TITLE = "GitHub Stars Index"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRY_ATTEMPTS = 3
BACKOFF_BASE = 2
BASE_RETRY_DELAY_SECONDS = 1.0
APPEND_BLOCK_BATCH_SIZE = 100
RETRYABLE_REQUEST_ERRORS = (requests.ConnectionError, requests.Timeout)
NOTION_RICH_TEXT_CONTENT_LIMIT = 2000
EMPTY_LANGUAGE_VALUES = {"N/A"}

DEFAULT_DATABASE_PROPERTIES = {
    "Repo": {"title": {}},
    "URL": {"url": {}},
    "Description": {"rich_text": {}},
    "Summary ZH": {"rich_text": {}},
    "Summary EN": {"rich_text": {}},
    "Language": {"select": {}},
    "Stars": {"number": {"format": "number"}},
    "Topics": {"multi_select": {}},
    "Starred At": {"date": {}},
    "Updated At": {"date": {}},
    "Pushed At": {"date": {}},
    "Homepage": {"url": {}},
    "Synced At": {"date": {}},
}


def _build_rich_text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def _extract_plain_text(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items)


def _default_database_properties() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(DEFAULT_DATABASE_PROPERTIES)


def _normalize_text(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    raise TypeError(f"{field_name} 必须是字符串或 None")


def _require_text(repo: Mapping[str, Any], field_name: str) -> str:
    value = _normalize_text(repo.get(field_name), field_name=field_name)
    if value is None:
        raise ValueError(f"repo['{field_name}'] 不能为空")
    return value


def _normalize_summary(repo: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = repo.get("summary")
    if summary is None:
        return {}
    if isinstance(summary, Mapping):
        return summary
    raise TypeError("repo['summary'] 必须是 dict")


def _summary_text(repo: Mapping[str, Any], language: str) -> Optional[str]:
    summary = _normalize_summary(repo)
    return _normalize_text(summary.get(language), field_name=f"summary.{language}")


def _normalize_language(value: Any) -> Optional[str]:
    language = _normalize_text(value, field_name="language")
    if language in EMPTY_LANGUAGE_VALUES:
        return None
    return language


def _normalize_number(value: Any, *, field_name: str) -> Optional[int | float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field_name} 不能是 boolean")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError as exc:
                raise ValueError(f"{field_name} 必须是数字") from exc
    raise TypeError(f"{field_name} 必须是数字或字符串")


def _normalize_topics(raw_topics: Any) -> list[str]:
    if raw_topics is None:
        return []
    if not isinstance(raw_topics, list):
        raise TypeError("repo['topics'] 必须是 list")
    topics: list[str] = []
    seen: set[str] = set()
    for topic in raw_topics:
        topic_name = _normalize_text(topic, field_name="topics[]")
        if topic_name is None or topic_name in seen:
            continue
        seen.add(topic_name)
        topics.append(topic_name)
    return topics


def _build_text_item(content: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": content}}


def _build_rich_text_items(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    chunks = _chunk_text(value, field_name=field_name)
    return [_build_text_item(chunk) for chunk in chunks]


def _build_rich_text_property(value: Any, *, field_name: str) -> dict[str, Any]:
    return {"rich_text": _build_rich_text_items(value, field_name=field_name)}


def _build_url_property(value: Any, *, field_name: str) -> dict[str, Any]:
    return {"url": _normalize_text(value, field_name=field_name)}


def _build_date_property(value: Any, *, field_name: str) -> dict[str, Any]:
    date_text = _normalize_text(value, field_name=field_name)
    return {"date": None if date_text is None else {"start": date_text}}


def _build_select_property(value: Any) -> dict[str, Any]:
    language = _normalize_language(value)
    return {"select": None if language is None else {"name": language}}


def _build_number_property(value: Any, *, field_name: str) -> dict[str, Any]:
    return {"number": _normalize_number(value, field_name=field_name)}


def _build_multi_select_property(raw_topics: Any) -> dict[str, Any]:
    topics = _normalize_topics(raw_topics)
    return {"multi_select": [{"name": topic} for topic in topics]}


def _join_non_empty_lines(lines: list[Optional[str]]) -> Optional[str]:
    normalized_lines = [line for line in lines if line is not None]
    if not normalized_lines:
        return None
    return "\n".join(normalized_lines)


def _chunk_text(
    value: Any, *, field_name: str, prefix: str = ""
) -> list[str]:
    text = _normalize_text(value, field_name=field_name)
    if text is None:
        return []
    if len(prefix) >= NOTION_RICH_TEXT_CONTENT_LIMIT:
        raise ValueError(f"{field_name} 的前缀长度超出 Notion 限制")
    chunks: list[str] = []
    first_limit = NOTION_RICH_TEXT_CONTENT_LIMIT - len(prefix or "")
    first_chunk = text[:first_limit]
    chunks.append(f"{prefix}{first_chunk}" if prefix else first_chunk)
    remaining = text[first_limit:]
    while remaining:
        chunks.append(remaining[:NOTION_RICH_TEXT_CONTENT_LIMIT])
        remaining = remaining[NOTION_RICH_TEXT_CONTENT_LIMIT:]
    return chunks


def _build_paragraph_block(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_build_text_item(content)]},
    }


def _build_paragraph_blocks(
    value: Any, *, field_name: str, prefix: str = ""
) -> list[dict[str, Any]]:
    chunks = _chunk_text(value, field_name=field_name, prefix=prefix)
    return [_build_paragraph_block(chunk) for chunk in chunks]


def _build_repo_header_text(repo: Mapping[str, Any]) -> str:
    repo_name = _require_text(repo, "full_name")
    repo_url = _normalize_text(repo.get("url"), field_name="url")
    header_text = _join_non_empty_lines(
        [f"仓库：{repo_name}", None if repo_url is None else f"链接：{repo_url}"]
    )
    if header_text is None:
        raise ValueError("仓库标题段不能为空")
    return header_text


def _build_meta_lines(repo: Mapping[str, Any]) -> list[str]:
    topics = _normalize_topics(repo.get("topics"))
    stars = _normalize_number(repo.get("stars"), field_name="stars")
    synced_at = _normalize_text(repo.get("synced_at"), field_name="synced_at")
    meta_pairs = [
        ("语言", _normalize_language(repo.get("language"))),
        ("Stars", None if stars is None else str(stars)),
        ("Topics", ", ".join(topics) if topics else None),
        ("Starred At", _normalize_text(repo.get("starred_at"), field_name="starred_at")),
        ("Updated At", _normalize_text(repo.get("updated_at"), field_name="updated_at")),
        ("Pushed At", _normalize_text(repo.get("pushed_at"), field_name="pushed_at")),
        ("Homepage", _normalize_text(repo.get("homepage"), field_name="homepage")),
        ("Synced At", synced_at),
    ]
    return [f"{label}: {value}" for label, value in meta_pairs if value is not None]


def _extract_repo_key(page: Mapping[str, Any]) -> str:
    page_id = _normalize_text(page.get("id"), field_name="page.id") or "<unknown>"
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        raise RuntimeError(f"Notion 页面缺少 properties: {page_id}")
    repo_property = properties.get("Repo")
    if not isinstance(repo_property, Mapping):
        raise RuntimeError(f"Notion 页面缺少 Repo 属性: {page_id}")
    if repo_property.get("type") != "title":
        raise RuntimeError(f"Notion 页面 Repo 属性类型错误: {page_id}")
    repo_key = _normalize_text(
        _extract_plain_text(repo_property.get("title", [])),
        field_name="Repo",
    )
    if repo_key is None:
        raise RuntimeError(f"Notion 页面 Repo 属性为空: {page_id}")
    return repo_key


def _extract_page_id(page: Mapping[str, Any]) -> str:
    page_id = _normalize_text(page.get("id"), field_name="page.id")
    if page_id is None:
        raise RuntimeError("Notion 页面缺少 id")
    return page_id


def _chunk_blocks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [
        blocks[index : index + APPEND_BLOCK_BATCH_SIZE]
        for index in range(0, len(blocks), APPEND_BLOCK_BATCH_SIZE)
    ]


def build_notion_properties(repo: dict) -> dict:
    return {
        "Repo": {
            "title": _build_rich_text_items(
                _require_text(repo, "full_name"),
                field_name="full_name",
            )
        },
        "URL": _build_url_property(repo.get("url"), field_name="url"),
        "Description": _build_rich_text_property(
            repo.get("description"),
            field_name="description",
        ),
        "Summary ZH": _build_rich_text_property(
            _summary_text(repo, "zh"),
            field_name="summary.zh",
        ),
        "Summary EN": _build_rich_text_property(
            _summary_text(repo, "en"),
            field_name="summary.en",
        ),
        "Language": _build_select_property(repo.get("language")),
        "Stars": _build_number_property(repo.get("stars"), field_name="stars"),
        "Topics": _build_multi_select_property(repo.get("topics")),
        "Starred At": _build_date_property(
            repo.get("starred_at"),
            field_name="starred_at",
        ),
        "Updated At": _build_date_property(
            repo.get("updated_at"),
            field_name="updated_at",
        ),
        "Pushed At": _build_date_property(
            repo.get("pushed_at"),
            field_name="pushed_at",
        ),
        "Homepage": _build_url_property(repo.get("homepage"), field_name="homepage"),
        "Synced At": _build_date_property(repo.get("synced_at"), field_name="synced_at"),
    }


def build_body_blocks(repo: dict) -> list[dict]:
    blocks = _build_paragraph_blocks(
        _build_repo_header_text(repo),
        field_name="repo_header",
    )
    blocks.extend(
        _build_paragraph_blocks(
            _summary_text(repo, "zh"),
            field_name="summary.zh",
            prefix="中文摘要：\n",
        )
    )
    blocks.extend(
        _build_paragraph_blocks(
            _summary_text(repo, "en"),
            field_name="summary.en",
            prefix="英文摘要：\n",
        )
    )
    blocks.extend(
        _build_paragraph_blocks(
            repo.get("description"),
            field_name="description",
            prefix="原始描述：\n",
        )
    )
    blocks.extend(
        _build_paragraph_blocks(
            _join_non_empty_lines(_build_meta_lines(repo)),
            field_name="meta",
            prefix="元信息：\n",
        )
    )
    return blocks


class NotionClient:
    BASE_URL = "https://api.notion.com/v1"

    def __init__(
        self,
        api_key: str,
        *,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_RETRY_ATTEMPTS,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.sleep = sleep
        self.timeout = timeout
        self.max_attempts = max_attempts

    def _request(
        self,
        method: str,
        path: str,
        *,
        before_retry: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        kwargs.setdefault("timeout", self.timeout)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(method=method, url=url, **kwargs)
            except RETRYABLE_REQUEST_ERRORS as exc:
                last_error = exc
                recovered = self._run_before_retry(before_retry)
                if recovered is not None:
                    return recovered
                if attempt == self.max_attempts:
                    raise RuntimeError(
                        f"Notion 请求失败: {method} {path}"
                    ) from exc
                self.sleep(self._retry_delay(attempt))
                continue
            except requests.RequestException as exc:
                raise RuntimeError(f"Notion 请求失败: {method} {path}") from exc

            if not self._is_retryable_status(response.status_code):
                return self._parse_response(response, method, path)
            recovered = self._run_before_retry(before_retry)
            if recovered is not None:
                return recovered
            if attempt == self.max_attempts:
                self._raise_response_error(response, method, path)
            self.sleep(self._retry_delay(attempt, response))
        raise RuntimeError(f"Notion 请求失败: {method} {path}") from last_error

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    def _run_before_retry(
        self, before_retry: Optional[Callable[[], Optional[dict[str, Any]]]]
    ) -> Optional[dict[str, Any]]:
        if before_retry is None:
            return None
        return before_retry()

    def _retry_delay(
        self, attempt: int, response: Optional[requests.Response] = None
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return BASE_RETRY_DELAY_SECONDS * (BACKOFF_BASE ** (attempt - 1))

    def _parse_response(
        self, response: requests.Response, method: str, path: str
    ) -> dict[str, Any]:
        if response.ok:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Notion 响应不是合法 JSON: {method} {path}"
                ) from exc
        self._raise_response_error(response, method, path)
        raise AssertionError("unreachable")

    def _raise_response_error(
        self, response: requests.Response, method: str, path: str
    ) -> None:
        detail = response.text.strip()
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("code") or detail
        raise RuntimeError(
            f"Notion API 错误: {method} {path} -> {response.status_code}: {detail}"
        )

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def create_database(
        self, parent_page_id: str, title: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": _build_rich_text(title),
            "description": _build_rich_text(OWNERSHIP_MARKER),
            "is_inline": True,
            "properties": properties,
        }
        return self._request(
            "POST",
            "/databases",
            json=payload,
            before_retry=lambda: self.find_existing_database(parent_page_id, title),
        )

    def search_databases(
        self, query: str, start_cursor: Optional[str] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "filter": {"property": "object", "value": "database"},
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", "/search", json=payload)

    def query_database(
        self, database_id: str, start_cursor: Optional[str] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", f"/databases/{database_id}/query", json=payload)

    def create_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/pages", json=payload)

    def update_page(self, page_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", json=payload)

    def list_block_children(
        self, block_id: str, start_cursor: Optional[str] = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._request("GET", f"/blocks/{block_id}/children", params=params)

    def append_block_children(
        self, block_id: str, children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/blocks/{block_id}/children",
            json={"children": children},
        )

    def delete_block(self, block_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/blocks/{block_id}")

    def find_existing_database(
        self, parent_page_id: str, title: str
    ) -> Optional[dict[str, Any]]:
        matches = self.find_matching_databases(parent_page_id, title)
        if len(matches) > 1:
            ids = ", ".join(database["id"] for database in matches)
            raise RuntimeError(f"父页面下存在多个同名 Notion Database: {title} ({ids})")
        if not matches:
            return None
        database = self.retrieve_database(matches[0]["id"])
        description = _extract_plain_text(database.get("description", []))
        if OWNERSHIP_MARKER not in description:
            raise RuntimeError(
                f"发现同名 Notion Database 但缺少 ownership marker: {title}"
            )
        return database

    def find_matching_databases(
        self, parent_page_id: str, title: str
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        next_cursor: Optional[str] = None
        while True:
            payload = self.search_databases(title, next_cursor)
            for database in payload.get("results", []):
                if database.get("object") != "database":
                    continue
                parent = database.get("parent", {})
                if parent.get("type") != "page_id":
                    continue
                if parent.get("page_id") != parent_page_id:
                    continue
                if _extract_plain_text(database.get("title", [])) == title:
                    matches.append(database)
            if not payload.get("has_more"):
                return matches
            next_cursor = payload.get("next_cursor")


class NotionSyncClient:
    def __init__(
        self,
        config: Mapping[str, Any],
        logger: Optional[logging.Logger] = None,
        *,
        client: Optional[NotionClient] = None,
    ) -> None:
        self.config = dict(config)
        self.logger = logger
        self.database_title = self.config.get("database_title", DEFAULT_DATABASE_TITLE)
        self.database_id = self.config.get("database_id")
        self.parent_page_id = self.config.get("page_id")
        self.client = client or self._build_client()
        self.database: Optional[dict[str, Any]] = None

    def ensure_database(self) -> dict[str, Any]:
        if self.database_id:
            database = self.client.retrieve_database(self.database_id)
            self._assert_marker(database, f"NOTION_DATABASE_ID={self.database_id}")
            self.database = database
            self._log("info", "使用显式配置的 Notion Database")
            return database

        if not self.parent_page_id:
            raise RuntimeError("缺少 NOTION_PAGE_ID，无法自动发现或创建 Notion Database")

        database = self.client.find_existing_database(
            self.parent_page_id, self.database_title
        )
        if database:
            self.database = database
            self._log("info", "复用已有 Notion Database")
            return database

        database = self.client.create_database(
            self.parent_page_id,
            self.database_title,
            _default_database_properties(),
        )
        self.database = database
        self._log("info", "创建新的 Notion Database")
        return database

    def load_existing_pages(self) -> dict[str, dict[str, Any]]:
        database = self.database or self.ensure_database()
        pages_by_repo: dict[str, dict[str, Any]] = {}
        next_cursor: Optional[str] = None
        while True:
            payload = self.client.query_database(database["id"], next_cursor)
            for page in payload.get("results", []):
                if page.get("object") != "page":
                    continue
                repo_key = _extract_repo_key(page)
                existing_page = pages_by_repo.get(repo_key)
                if existing_page is not None:
                    existing_id = existing_page.get("id", "<unknown>")
                    page_id = page.get("id", "<unknown>")
                    raise RuntimeError(
                        f"发现重复 Repo 页面: {repo_key} ({existing_id}, {page_id})"
                    )
                pages_by_repo[repo_key] = page
            if not payload.get("has_more"):
                return pages_by_repo
            next_cursor = payload.get("next_cursor")

    def sync(
        self,
        ordered_repos: list[Mapping[str, Any]],
        test_limit: Optional[int] = None,
        has_live_star_source: bool = True,
    ) -> None:
        self.ensure_database()
        existing_pages = self.load_existing_pages()
        current_repo_names: set[str] = set()
        for repo in ordered_repos:
            repo_name = _require_text(repo, "full_name")
            current_repo_names.add(repo_name)
            existing_page = existing_pages.get(repo_name)
            if existing_page is None:
                existing_pages[repo_name] = self._create_repo_page(repo)
                self._log("info", f"Notion create: {repo_name}")
                continue
            if self._is_page_archived(existing_page):
                self._set_page_archived(_extract_page_id(existing_page), archived=False)
                self._log("info", f"Notion unarchive: {repo_name}")
            self._update_repo_page(_extract_page_id(existing_page), repo)
            self._log("info", f"Notion update: {repo_name}")
        if test_limit is not None:
            self._log("info", "TEST_LIMIT active, archive skipped")
            return
        if not has_live_star_source:
            self._log("info", "Not live source (render-only), archive skipped")
            return
        self.archive_missing_repos(existing_pages, current_repo_names)

    def archive_missing_repos(
        self,
        existing_pages: Mapping[str, dict[str, Any]],
        current_repo_names: set[str],
    ) -> None:
        for repo_name, page in existing_pages.items():
            if repo_name in current_repo_names or self._is_page_archived(page):
                continue
            self._set_page_archived(_extract_page_id(page), archived=True)
            self._log("info", f"Notion archive: {repo_name}")

    def _create_repo_page(self, repo: Mapping[str, Any]) -> dict[str, Any]:
        body_blocks = build_body_blocks(dict(repo))
        payload = {
            "parent": {"database_id": self._database_id()},
            "properties": build_notion_properties(dict(repo)),
        }
        page = self.client.create_page(payload)
        self._append_body_blocks(_extract_page_id(page), body_blocks)
        return page

    def _update_repo_page(self, page_id: str, repo: Mapping[str, Any]) -> None:
        self.client.update_page(
            page_id,
            {"properties": build_notion_properties(dict(repo))},
        )

    def _append_body_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        for batch in _chunk_blocks(blocks):
            self.client.append_block_children(page_id, batch)

    def _set_page_archived(self, page_id: str, *, archived: bool) -> None:
        self.client.update_page(page_id, {"archived": archived})

    def _database_id(self) -> str:
        database = self.database or self.ensure_database()
        database_id = _normalize_text(database.get("id"), field_name="database.id")
        if database_id is None:
            raise RuntimeError("Notion Database 缺少 id")
        return database_id

    def _is_page_archived(self, page: Mapping[str, Any]) -> bool:
        return bool(page.get("archived") or page.get("in_trash"))

    def _build_client(self) -> NotionClient:
        api_key = self.config.get("api_key")
        if not api_key:
            raise RuntimeError("缺少 NOTION_API_KEY，无法初始化 Notion 客户端")
        return NotionClient(api_key)

    def _assert_marker(self, database: dict[str, Any], source: str) -> None:
        description = _extract_plain_text(database.get("description", []))
        if OWNERSHIP_MARKER in description:
            return
        raise RuntimeError(
            f"Notion Database 不属于脚本专用库: {source} 缺少 ownership marker"
        )

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        log_method = getattr(self.logger, level, None)
        if callable(log_method):
            log_method(message)


__all__ = [
    "NOTION_VERSION",
    "OWNERSHIP_MARKER",
    "build_notion_properties",
    "build_body_blocks",
    "NotionClient",
    "NotionSyncClient",
]
