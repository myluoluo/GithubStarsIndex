#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Stars Index同步脚本 (JSON + Template 版)
功能：
  1. 从 GitHub API 抓取用户 Star 的项目列表
  2. 增量获取 README 并调用 AI 生成摘要，存储至 JSON 数据集
  3. 使用 Jinja2 模板将 JSON 数据渲染为 Markdown
  4. 支持推送到 Obsidian Vault 仓库
"""

import os
import sys
import json
import re
import time
import base64
import logging
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync_targets import NotionSyncClient, VaultSyncClient

# 加载本地 .env 文件
load_dotenv(override=True)

# ── 日志配置 ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# ── 常量 ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.parent  # 仓库根目录
CONFIG_PATH = SCRIPT_DIR / "config.yml"
DATA_DIR = SCRIPT_DIR / "data"
STARS_JSON_PATH = DATA_DIR / "stars.json"
TEMPLATES_DIR = SCRIPT_DIR / "templates"
DEFAULT_MD_TEMPLATE = "stars.md.j2"
STARS_MD_PATH_DEFAULT = SCRIPT_DIR / "stars.md"
ROBOTS_TXT_NAME = "robots.txt"
ROBOTS_TXT_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
EMPTY_SNAPSHOT_MAX_ATTEMPTS = 3
ALLOW_EMPTY_SNAPSHOT_ENV = "ALLOW_EMPTY_GITHUB_STARS_SNAPSHOT"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════


BOOLEAN_ENV_KEYS = {
    "VAULT_SYNC_ENABLED",
    "PAGES_SYNC_ENABLED",
    "PAGES_DISALLOW_INDEXING",
    "NOTION_SYNC_ENABLED",
    ALLOW_EMPTY_SNAPSHOT_ENV,
}
INTEGER_ENV_KEYS = {"MAX_CONCURRENCY", "TEST_LIMIT"}
INTEGER_ENV_KEYS.add("AI_TIMEOUT")
SUPPORTED_AI_API_STYLES = {"chat_completions", "responses"}


def _parse_env_value(env_key: str, raw_value: str):
    if env_key in INTEGER_ENV_KEYS:
        return int(raw_value) if raw_value.isdigit() else None
    if env_key in BOOLEAN_ENV_KEYS:
        return raw_value.lower() == "true"
    return raw_value


def _set_config_value(cfg: dict, config_path: str, value) -> None:
    parts = config_path.split(".")
    target = cfg
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def _validate_ai_api_style(raw_value: Optional[str]) -> str:
    api_style = (raw_value or "").strip().lower()
    if not api_style:
        return "chat_completions"
    if api_style in SUPPORTED_AI_API_STYLES:
        return api_style
    raise ValueError(
        "AI_API_STYLE 仅支持 chat_completions 或 responses"
    )


def load_config() -> dict:
    """加载配置：环境变量优先于 config.yml"""
    env_mapping = {
        "GH_USERNAME": "github.username",
        "GH_TOKEN": "github.token",
        "GITHUB_TOKEN": "github.token",
        ALLOW_EMPTY_SNAPSHOT_ENV: "github.allow_empty_snapshot",
        "AI_BASE_URL": "ai.base_url",
        "AI_API_KEY": "ai.api_key",
        "AI_MODEL": "ai.model",
        "AI_API_STYLE": "ai.api_style",
        "AI_TIMEOUT": "ai.timeout",
        "AI_USER_AGENT": "ai.user_agent",
        "MAX_CONCURRENCY": "ai.concurrency",
        "OUTPUT_FILENAME": "output.filename",
        "VAULT_SYNC_ENABLED": "vault_sync.enabled",
        "VAULT_REPO": "vault_sync.repo",
        "VAULT_SYNC_PATH": "vault_sync.path",
        "VAULT_PAT": "vault_sync.pat",
        "PAGES_SYNC_ENABLED": "pages_sync.enabled",
        "PAGES_DISALLOW_INDEXING": "pages_sync.disallow_indexing",
        "NOTION_SYNC_ENABLED": "notion_sync.enabled",
        "NOTION_API_KEY": "notion_sync.api_key",
        "NOTION_PAGE_ID": "notion_sync.page_id",
        "NOTION_DATABASE_ID": "notion_sync.database_id",
        "NOTION_DATABASE_TITLE": "notion_sync.database_title",
        "TEST_LIMIT": "test_limit",
    }

    cfg = {
        "github": {
            "username": os.environ.get("GH_USERNAME"),
            "token": None,
            "allow_empty_snapshot": False,
        },
        "ai": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": None,
            "api_style": "chat_completions",
            "timeout": 60,
            "user_agent": None,
            "concurrency": 5,
        },
        "output": {"filename": "stars"},
        "vault_sync": {
            "enabled": False,
            "repo": None,
            "path": "GitHub-Stars/",
            "pat": None,
            "commit_message": "🤖 自动更新 GitHub Stars 摘要",
        },
        "pages_sync": {"enabled": False, "disallow_indexing": False},
        "notion_sync": {
            "enabled": False,
            "api_key": None,
            "page_id": None,
            "database_id": None,
            "database_title": "GitHub Stars Index",
        },
        "test_limit": None,
    }

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_yml = yaml.safe_load(f) or {}
            for section in ["ai", "output", "vault_sync", "pages_sync", "notion_sync"]:
                if section in user_yml and isinstance(user_yml[section], dict):
                    cfg[section].update(user_yml[section])

    for env_key, config_path in env_mapping.items():
        raw_value = os.environ.get(env_key)
        if raw_value is None:
            continue
        value = _parse_env_value(env_key, raw_value)
        if value is None:
            continue
        _set_config_value(cfg, config_path, value)

    try:
        cfg["ai"]["api_style"] = _validate_ai_api_style(
            cfg["ai"].get("api_style")
        )
    except ValueError as error:
        log.error(f"❌ 错误: {error}")
        sys.exit(1)

    if not cfg["github"]["username"]:
        log.error("❌ 错误: 未配置 GitHub 用户名 (GH_USERNAME)")
        sys.exit(1)
    if not cfg["ai"]["api_key"]:
        log.error("❌ 错误: 未配置 AI API Key (AI_API_KEY)")
        sys.exit(1)
    if cfg["notion_sync"]["enabled"] and not cfg["notion_sync"]["api_key"]:
        log.error("❌ 错误: 未配置 Notion API Key (NOTION_API_KEY)")
        sys.exit(1)
    if cfg["notion_sync"]["enabled"]:
        has_database_id = bool(cfg["notion_sync"]["database_id"])
        has_page_id = bool(cfg["notion_sync"]["page_id"])
        if not has_database_id and not has_page_id:
            log.error(
                "❌ 错误: 至少需要配置 NOTION_DATABASE_ID 或 NOTION_PAGE_ID 其中之一"
            )
            sys.exit(1)

    return cfg


# ════════════════════════════════════════════════════════════
# 数据存储
# ════════════════════════════════════════════════════════════


class DataStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"last_updated": "", "repos": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"加载数据文件失败: {e}")
            return {"last_updated": "", "repos": {}}

    def save(self):
        with self.lock:
            self.data["last_updated"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_repo(self, full_name: str, metadata: dict, summary: dict):
        with self.lock:
            self.data["repos"][full_name] = {
                "metadata": metadata,
                "summary": summary,
                "pushed_at": metadata.get("pushed_at", ""),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }

    def get_repo(self, full_name: str) -> Optional[dict]:
        return self.data["repos"].get(full_name)


def prune_removed_repos(store: DataStore, live_repo_names: set[str]) -> int:
    existing_repo_names = set(store.data.get("repos", {}))
    removed_repo_names = existing_repo_names - live_repo_names
    for repo_name in removed_repo_names:
        store.data["repos"].pop(repo_name, None)
    return len(removed_repo_names)


def has_persistable_changes(
    *, new_count: int, refreshed_count: int, removed_count: int
) -> bool:
    return any((new_count, refreshed_count, removed_count))


def get_cached_repo_count(store: DataStore) -> int:
    return len(store.data.get("repos", {}))


def ensure_live_snapshot_is_safe(
    *,
    live_repo_count: int,
    cached_repo_count: int,
    allow_empty_snapshot: bool,
) -> None:
    if live_repo_count > 0 or cached_repo_count == 0 or allow_empty_snapshot:
        return
    raise RuntimeError(
        "GitHub Stars 返回空列表，但本地缓存中仍有历史数据。"
        "为避免覆盖 data/stars.json，本次同步已中止。"
        f"如确认当前账号确实没有任何 Stars，请显式设置 "
        f"{ALLOW_EMPTY_SNAPSHOT_ENV}=true。"
    )


def fetch_live_star_snapshot(
    gh: "GitHubClient",
    *,
    cached_repo_count: int,
    allow_empty_snapshot: bool,
) -> list[dict]:
    for attempt in range(1, EMPTY_SNAPSHOT_MAX_ATTEMPTS + 1):
        repos = gh.get_starred_repos()
        live_repo_count = len(repos)
        log.info(
            "📥 Live Stars 抓取结果: 第 %s/%s 次尝试获取 %s 条，本地缓存 %s 条",
            attempt,
            EMPTY_SNAPSHOT_MAX_ATTEMPTS,
            live_repo_count,
            cached_repo_count,
        )
        if live_repo_count > 0:
            return repos
        if cached_repo_count == 0:
            log.warning("⚠️ GitHub Stars 返回空列表，且本地缓存也为空，将按空数据继续。")
            return repos
        if allow_empty_snapshot:
            log.warning(
                "⚠️ GitHub Stars 返回空列表，但已显式允许空快照，将按空数据继续。"
            )
            return repos
        if attempt == EMPTY_SNAPSHOT_MAX_ATTEMPTS:
            break
        wait_seconds = 2 ** (attempt - 1)
        log.warning(
            "⚠️ GitHub Stars 返回空列表，本次不会直接覆盖历史数据。"
            " %s 秒后进行第 %s 次重试。",
            wait_seconds,
            attempt + 1,
        )
        time.sleep(wait_seconds)

    ensure_live_snapshot_is_safe(
        live_repo_count=0,
        cached_repo_count=cached_repo_count,
        allow_empty_snapshot=allow_empty_snapshot,
    )
    return []


def sync_pages_robots_txt(output_dir: Path, disallow_indexing: bool) -> None:
    robots_path = output_dir / ROBOTS_TXT_NAME
    if disallow_indexing:
        robots_path.write_text(ROBOTS_TXT_DISALLOW_ALL, encoding="utf-8")
        log.info(f"✅ robots.txt 已生成: {robots_path}")
        return
    if robots_path.exists():
        robots_path.unlink()
        log.info(f"🧹 已移除 robots.txt: {robots_path}")


# ════════════════════════════════════════════════════════════
# GitHub API 客户端
# ════════════════════════════════════════════════════════════


class GitHubClient:
    BASE_URL = "https://api.github.com"
    MAX_REQUEST_ATTEMPTS = 3

    def __init__(self, username: str, token: Optional[str] = None):
        self.username = username
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(
        self, url: str, params: dict = None, headers: Optional[dict] = None
    ) -> requests.Response:
        for attempt in range(self.MAX_REQUEST_ATTEMPTS):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=30
                )
                if resp.status_code == 403 and "rate limit" in resp.text.lower():
                    reset_time = int(
                        resp.headers.get("X-RateLimit-Reset", time.time() + 60)
                    )
                    wait = max(reset_time - int(time.time()), 5)
                    log.warning(f"API 限速，等待 {wait} 秒...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.HTTPError as e:
                status_code = getattr(e.response, "status_code", None)
                if status_code is not None and status_code < 500 and status_code != 429:
                    raise
                log.warning(f"请求失败（第 {attempt + 1} 次）: {e}")
            except requests.RequestException as e:
                log.warning(f"请求失败（第 {attempt + 1} 次）: {e}")
            if attempt == self.MAX_REQUEST_ATTEMPTS - 1:
                raise Exception("多次请求失败")
            time.sleep(2**attempt)

    def get_starred_repos(self) -> list[dict]:
        repos = []
        page = 1
        log.info(f"正在抓取 @{self.username} 的 Stars...")
        while True:
            url = f"{self.BASE_URL}/users/{self.username}/starred"
            resp = self._get(
                url,
                params={
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                },
                headers={"Accept": "application/vnd.github.star+json"},
            )
            data = resp.json()
            if not data:
                log.warning(
                    "⚠️ GitHub Stars API 第 %s 页返回空列表，当前累计 %s 条",
                    page,
                    len(repos),
                )
                break
            for item in data:
                repo = item.get("repo", item)
                starred_at_raw = item.get("starred_at", "")
                repos.append(
                    {
                        "full_name": repo["full_name"],
                        "name": repo["name"],
                        "owner": repo["owner"]["login"],
                        "description": repo.get("description") or "",
                        "stars": repo["stargazers_count"],
                        "language": repo.get("language") or "N/A",
                        "url": repo["html_url"],
                        "homepage": repo.get("homepage") or "",
                        "topics": repo.get("topics", []),
                        "pushed_at": repo.get("pushed_at", "") or "",
                        "updated_at": repo.get("updated_at", "") or "",
                        "starred_at": starred_at_raw or "",
                    }
                )
            log.info(f"  第 {page} 页：获取 {len(data)} 个，共 {len(repos)} 个")
            if "next" not in resp.headers.get("Link", ""):
                break
            page += 1
        return repos

    def get_readme(self, full_name: str, max_length: int) -> str:
        url = f"{self.BASE_URL}/repos/{full_name}/readme"
        try:
            resp = self._get(url)
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            return content[:max_length]
        except Exception:
            return ""

    def push_file(self, repo: str, path: str, content: str, msg: str, pat: str) -> bool:
        url = f"{self.BASE_URL}/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        }
        sha = None
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception:
            pass
        payload = {
            "message": msg,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        try:
            r = requests.put(url, headers=headers, json=payload, timeout=30)
            r.raise_for_status()
            log.info(f"✅ 已推送至: {repo}/{path}")
            return True
        except Exception as e:
            log.error(f"❌ 推送失败: {e}")
            return False


# ── 标签治理配置 ──────────────────────────────────────────
TAG_MAPPING = {
    # ── AI & 大模型 ──
    "LLM": "AI 大模型",
    "Large Language Model": "AI 大模型",
    "Agent": "AI 智能体",
    "Agents": "AI 智能体",
    "AI Agent": "AI 智能体",
    "Generative AI": "生成式 AI",
    "Prompt Engineering": "提示工程",
    "LangChain": "LangChain 框架",
    "RAG": "RAG 检索增强",
    "Stable Diffusion": "AI 图像生成",
    "Image Generation": "AI 图像生成",
    "Text-to-Image": "AI 图像生成",
    "Computer Vision": "计算机视觉",
    "NLP": "自然语言处理",
    "Vector Database": "向量数据库",
    "Fine-tuning": "模型微调",
    "Quantization": "模型量化",
    "Multi-modal": "多模态 AI",
    "Deep Learning": "深度学习",
    "Machine Learning": "机器学习",
    
    # ── 技术栈归一化 ──
    "JS": "JavaScript",
    "TS": "TypeScript",
    "Golang": "Go",
    "Rustlang": "Rust",
    "Vuejs": "Vue.js",
    "Reactjs": "React",
    "Nextjs": "Next.js",
    "Nuxtjs": "Nuxt.js",
    "SvelteKit": "Svelte",
    "TailwindCSS": "Tailwind CSS",
    "Tailwind": "Tailwind CSS",
    "Tauri": "Tauri 框架",
    "Flutter": "Flutter",
    "ReactNative": "React Native",
    "FastAPI": "FastAPI",
    "Django": "Django",
    "Flask": "Flask",
    "SpringBoot": "Spring Boot",
    "Postgres": "PostgreSQL",
    "Redis": "Redis",
    "MongoDB": "MongoDB",
    "SQLite": "SQLite",
    
    # ── 领域/场景 ──
    "Web Scraping": "网页爬虫",
    "Crawler": "网页爬虫",
    "Automation": "自动化工具",
    "DevOps": "运维自动化",
    "Cybersecurity": "网络安全",
    "Data Visualization": "数据可视化",
    "Knowledge Graph": "知识图谱",
    "Microservices": "微服务架构",
    "Docker": "容器化",
    "Kubernetes": "Kubernetes",
    "K8s": "Kubernetes",
    "Serverless": "无服务器",
    "Cloud Computing": "云计算",
    
    # ── 平台与应用类型 ──
    "Browser Extension": "浏览器插件",
    "Chrome Extension": "浏览器插件",
    "VS Code Extension": "VS Code 插件",
    "Desktop App": "桌面应用",
    "Mobile App": "移动端应用",
    "CLI": "命令行工具",
    "Terminal": "命令行工具",
    "API": "开发者接口",
    "SDK": "开发者 SDK",
    
    # ── 其它归口 ──
    "Awesome": "精选资源",
    "Tutorial": "技术教程",
    "Library": "开发库",
    "Framework": "核心框架",
    "Boilerplate": "项目模板",
    "Template": "项目模板",
    "Privacy": "隐私安全",
    "Self-hosted": "私有化部署",
}

# ════════════════════════════════════════════════════════════
# AI 摘要生成
# ════════════════════════════════════════════════════════════

class AISummarizer:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_style: str = "chat_completions",
        timeout: int = 60,
        retry: int = 3,
        user_agent: Optional[str] = None,
    ):
        self.base_url = (base_url or "").lower()
        self.model = model
        self.api_style = _validate_ai_api_style(api_style)
        self.retry = retry
        default_headers = self._build_default_headers(user_agent)
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            default_headers=default_headers,
        )

    def _build_default_headers(
        self, user_agent: Optional[str]
    ) -> Optional[dict[str, str]]:
        if not user_agent:
            return None
        return {"User-Agent": user_agent}

    def normalize_tags(self, tags: list[str]) -> list[str]:
        """标签归一化：去重、合并同义词、统一大小写"""
        normalized = set()
        for t in tags:
            # 1. 基础清洗
            t = t.strip()
            if not t: continue
            
            # 2. 查表合并
            # 尝试 原词、全小写、全大写 的匹配
            mapped = TAG_MAPPING.get(t) or TAG_MAPPING.get(t.upper()) or TAG_MAPPING.get(t.title())
            if mapped:
                normalized.add(mapped)
            else:
                # 3. 兜底处理：如果不在字典里，仅做基础美化
                normalized.add(t)
        
        return sorted(list(normalized))

    def _extract_json_payload(self, content: object) -> dict:
        if content is None:
            raise ValueError("empty content")

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            text = "\n".join(parts).strip()
        else:
            text = str(content).strip()

        if not text:
            raise ValueError("empty content")

        # MiniMax 兼容接口可能会把 CoT 放在 <think>...</think> 中，先剥离。
        text = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL).sub("", text).strip()

        # 兼容 ```json ... ``` 包裹场景。
        text = text.replace("```json", "```").strip()
        if text.startswith("```") and text.endswith("```"):
            text = text[3:-3].strip()

        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(text[idx:])
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue

        raise ValueError("no valid json object found in model content")

    def _build_response_request(self, prompt: str, context: str) -> dict:
        request = {
            "model": self.model,
            "instructions": prompt,
            "input": context,
            "temperature": 0.3,
        }
        if not self._uses_minimax_compatible_api():
            request["text"] = {"format": {"type": "json_object"}}
        return request

    def _build_chat_completion_request(self, prompt: str, context: str) -> dict:
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": context},
            ],
            "temperature": 0.3,
        }
        if not self._uses_minimax_compatible_api():
            request["response_format"] = {"type": "json_object"}
        return request

    def _extract_response_text(self, response: object) -> object:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return output_text

        output_items = getattr(response, "output", None)
        if not isinstance(output_items, list):
            return output_text

        parts = []
        for item in output_items:
            content_items = getattr(item, "content", None)
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                text = getattr(content, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()

    def _extract_chat_completion_text(self, response: object) -> object:
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            return None
        message = getattr(choices[0], "message", None)
        if message is None:
            return None
        return getattr(message, "content", None)

    def _uses_minimax_compatible_api(self) -> bool:
        return "api.minimax.io" in self.base_url or "api.minimaxi.com" in self.base_url

    def _summarize_with_responses(self, prompt: str, context: str) -> dict:
        response = self.client.responses.create(
            **self._build_response_request(prompt, context)
        )
        return self._extract_json_payload(self._extract_response_text(response))

    def _summarize_with_chat_completions(self, prompt: str, context: str) -> dict:
        response = self.client.chat.completions.create(
            **self._build_chat_completion_request(prompt, context)
        )
        return self._extract_json_payload(
            self._extract_chat_completion_text(response)
        )

    def _request_summary_payload(self, prompt: str, context: str) -> dict:
        if self.api_style == "responses":
            return self._summarize_with_responses(prompt, context)
        return self._summarize_with_chat_completions(prompt, context)

    def summarize(self, repo_name: str, description: str, readme: str) -> dict:
        context = f"Repo: {repo_name}\nDesc: {description}\n\nREADME:\n{readme}"
        prompt = """你是一个顶级技术布道师和架构师。请深入分析 GitHub 仓库信息并生成：
1. **中文摘要**（80-100字）：准确提炼核心价值、应用场景与技术亮点，避免空话。
2. **英文摘要**（80-100字）。
3. **高权重标签**（中英文各 2-4 个）：
   - **分层打标**：[1个领域大类] + [1-2个核心技术栈] + [1个核心用途]。
   - **优先选用标准词**：例如 AI智能体, AI大模型, 网页爬虫, 自动化工具, 提示工程, 运维自动化, 跨平台, 数据可视化, 知识图谱, 浏览器插件, 桌面应用。
   - **严控数量**：标签必须是极高权重的，禁止琐碎或重复（如不要同时打 "Python" 和 "Python脚本"）。
   - **语言规范**：中文标签尽量简洁有力，英文标签首字母大写。

输出 JSON 格式：
{
  "zh": "中文摘要",
  "en": "English summary",
  "tags_zh": ["应用领域", "核心技术", "主要特征"],
  "tags_en": ["Domain", "Tech Stack", "Key Feature"]
}"""
        for attempt in range(self.retry):
            try:
                data = self._request_summary_payload(prompt, context)
                # 兼容性处理
                if "tags" in data and "tags_zh" not in data:
                    data["tags_zh"] = data["tags"]
                
                # 标签归一化治理
                data["tags_zh"] = self.normalize_tags(data.get("tags_zh", []))
                data["tags_en"] = self.normalize_tags(data.get("tags_en", []))
                
                return data
            except Exception as e:
                if attempt == self.retry - 1:
                    log.error(f"AI 生成失败 [{repo_name}]: {e}")
                    return {
                        "zh": "生成失败",
                        "en": "Generation failed",
                        "tags_zh": [],
                        "tags_en": [],
                    }
                log.warning(f"AI 生成失败 [{repo_name}]，重试中 {attempt + 1}: {e}")
                time.sleep(2**attempt)


# ════════════════════════════════════════════════════════════
# 模版生成器
# ════════════════════════════════════════════════════════════


class TemplateGenerator:
    def __init__(self, template_dir: Path):
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # 添加简单的 JS 转义过滤器
        self.env.filters["escapejs"] = (
            lambda x: x.replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")
        )

    def render(self, template_name: str, context: dict) -> str:
        template = self.env.get_template(template_name)
        return template.render(context)


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="GitHub Stars Index 同步与生成脚本")
    parser.add_argument(
        "--render-only", action="store_true", help="直接使用本地 json 数据重新生成展示文件，不进行 API 抓取"
    )
    args = parser.parse_args()

    log.info("GitHub Stars Index同步系统开始运行")
    cfg = load_config()
    gh = GitHubClient(cfg["github"]["username"], cfg["github"].get("token"))

    store = DataStore(STARS_JSON_PATH)
    cached_repo_count = get_cached_repo_count(store)
    log.info(f"📦 已加载本地缓存，共 {cached_repo_count} 条记录")
    generator = TemplateGenerator(TEMPLATES_DIR)

    if args.render_only:
        log.info("🚀 运行模式: 仅渲染 (Render Only)")
        if not STARS_JSON_PATH.exists():
            log.error(f"❌ 错误: 未找到数据文件 {STARS_JSON_PATH}，无法进行渲染。")
            sys.exit(1)
        # 在仅渲染模式下，我们从 store 中获取所有已处理的项目
        # 注意：这里我们没有 gh 客户端抓取的实时 all_repos 顺序，
        # 所以我们按照 json 中存储的 pushed_at 倒序排列，模拟原有逻辑。
        all_repos_data = []
        for full_name, entry in store.data.get("repos", {}).items():
            repo_meta = entry.get("metadata", {})
            if not repo_meta:
                continue
            all_repos_data.append(repo_meta)

        # 优先按 star 时间排序，缺失时回退到更新时间
        all_repos = sorted(
            all_repos_data,
            key=lambda x: x.get("updated_at")
            or x.get("pushed_at", ""),
            reverse=True,
        )
        log.info(f"🖼️ 仅渲染模式共装载 {len(all_repos)} 条仓库记录")
    else:
        ai = AISummarizer(
            cfg["ai"]["base_url"],
            cfg["ai"]["api_key"],
            cfg["ai"]["model"],
            cfg["ai"].get("api_style", "chat_completions"),
            cfg["ai"].get("timeout", 60),
            cfg["ai"].get("max_retries", 3),
            cfg["ai"].get("user_agent"),
        )

        # 1. 抓取所有 Stars
        all_repos = fetch_live_star_snapshot(
            gh,
            cached_repo_count=cached_repo_count,
            allow_empty_snapshot=bool(cfg["github"].get("allow_empty_snapshot")),
        )

        # 2. 增量处理
        new_repos_to_process = []
        refreshed_count = 0
        seen_full_names = set()  # 防止 API 返回重复数据
        test_limit = cfg.get("test_limit")

        for repo in all_repos:
            full_name = repo["full_name"]

            if full_name in seen_full_names:
                continue

            existing = store.get_repo(full_name)

            is_processed = False
            if existing:
                summ = existing.get("summary", {})
                if summ and summ.get("zh") and "生成失败" not in summ.get("zh"):
                    is_processed = True

            if not is_processed:
                if test_limit is not None and len(new_repos_to_process) >= test_limit:
                    continue
                new_repos_to_process.append(repo)
                seen_full_names.add(full_name)
            else:
                existing["metadata"] = repo
                refreshed_count += 1
                seen_full_names.add(full_name)

        log.info(
            "📊 同步分类统计: live=%s，待生成=%s，已复用=%s，去重后=%s，TEST_LIMIT=%s",
            len(all_repos),
            len(new_repos_to_process),
            refreshed_count,
            len(seen_full_names),
            test_limit if test_limit is not None else "off",
        )

        def process_repo(args_tuple):
            idx, repo_data = args_tuple
            fname = repo_data["full_name"]
            total = len(new_repos_to_process)

            log.info(f"[{idx}/{total}] 正在处理新仓库: {fname}")
            readme_content = gh.get_readme(
                fname, cfg["ai"].get("max_readme_length", 4000)
            )

            if not readme_content and not repo_data["description"]:
                summ = {"zh": "暂无描述。", "tags": []}
            else:
                summ = ai.summarize(fname, repo_data["description"], readme_content)

            store.update_repo(fname, repo_data, summ)
            return True

        new_count = len(new_repos_to_process)
        if new_count > 0:
            concurrency = cfg["ai"].get("concurrency", 5)
            log.info(f"🚀 开始并发处理 {new_count} 个新仓库 (并发数: {concurrency})")
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                list(executor.map(process_repo, enumerate(new_repos_to_process, 1)))

        removed_count = 0
        if test_limit is None:
            removed_count = prune_removed_repos(store, seen_full_names)
            if removed_count > 0:
                log.info(f"🧹 已清理 {removed_count} 条不再属于当前 Stars 的历史记录")

        if has_persistable_changes(
            new_count=new_count,
            refreshed_count=refreshed_count,
            removed_count=removed_count,
        ):
            store.save()
            log.info(
                f"✅ 数据保存完成，新增 {new_count} 条记录，"
                f"刷新 {refreshed_count} 条，清理 {removed_count} 条"
            )
        else:
            log.info(
                "✨ 没有可持久化变更: live=%s，缓存=%s，已复用=%s",
                len(all_repos),
                cached_repo_count,
                refreshed_count,
            )
    has_live_star_source = not args.render_only

    # 3. 按 Star 时间重新排序（最新 Star 在前）
    # JSON 里的 repos 是无序的，我们按照 all_repos 的顺序来生成（它是倒序的）
    ordered_repos = []
    for r_meta in all_repos:
        entry = store.get_repo(r_meta["full_name"])
        if entry:
            # 确保 summary 格式正确，防止旧数据或空数据导致模版崩溃
            summary = entry.get("summary") or {}
            if not isinstance(summary, dict):
                summary = {"zh": str(summary), "tags": []}

            # 补全缺失字段
            summary.setdefault("zh", "暂无摘要")
            summary.setdefault("en", summary.get("zh", "No summary available"))
            summary.setdefault("tags_zh", summary.get("tags", []))
            summary.setdefault("tags_en", summary.get("tags", []))

            # 合并展示需要的数据
            view_data = {**entry["metadata"], "summary": summary}
            ordered_repos.append(view_data)

    # 4. 统计语言分布 (取前 5)
    lang_stats = {}
    for r in ordered_repos:
        lang = r.get("language")
        if lang:
            lang_stats[lang] = lang_stats.get(lang, 0) + 1

    # 转换为排序后的列表: [{"name": "Python", "count": 10}, ...]
    top_langs = sorted(
        [{"name": k, "count": v} for k, v in lang_stats.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # 5. 渲染 Markdown (多语言版本)
    context = {
        "last_updated": store.data["last_updated"],
        "repos": ordered_repos,
        "top_langs": top_langs,
        "ai_model": cfg["ai"].get("model", "gpt-4o-mini"),
    }
    langs = ["zh", "en"]
    generated_mds = {}

    # 确保 dist 目录存在
    dist_dir = SCRIPT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)

    for lang in langs:
        lang_context = {**context, "current_lang": lang}
        base_name = cfg["output"].get("filename", "stars")
        output_name = f"{base_name}_{lang}.md"

        # 直接写入 dist 目录
        output_md_path = dist_dir / output_name
        md_content = generator.render(DEFAULT_MD_TEMPLATE, lang_context)

        # 物理写入磁盘
        output_md_path.write_text(md_content, encoding="utf-8")

        generated_mds[lang] = {"path": output_md_path, "content": md_content}
        log.info(f"✅ Markdown ({lang}) 生成完成: {output_md_path}")

    # 6. 可选：Notion 同步
    n_cfg = cfg.get("notion_sync", {})
    if n_cfg.get("enabled"):
        notion_sync = NotionSyncClient(n_cfg, log)
        notion_sync.sync(
            ordered_repos,
            cfg.get("test_limit"),
            has_live_star_source=has_live_star_source,
        )

    # 7. 可选：Vault 同步
    v_cfg = cfg.get("vault_sync", {})
    if v_cfg.get("enabled"):
        vault_sync = VaultSyncClient(gh, cfg["output"].get("filename", "stars"))
        vault_sync.sync(v_cfg, generated_mds)

    # 8. 可选：GitHub Pages 生成
    p_cfg = cfg.get("pages_sync", {})
    if p_cfg.get("enabled"):
        try:
            out_dir = SCRIPT_DIR / p_cfg.get("output_dir", "dist")
            out_dir.mkdir(exist_ok=True)
            sync_pages_robots_txt(
                out_dir,
                bool(p_cfg.get("disallow_indexing")),
            )

            html_template = p_cfg.get("template", "index.html.j2")
            html_content = generator.render(html_template, context)

            html_path = out_dir / p_cfg.get("file_name", "index.html")
            html_path.write_text(html_content, encoding="utf-8")
            log.info(f"✅ HTML 生成完成: {html_path}")
        except Exception as e:
            log.error(f"❌ HTML 生成失败: {e}")

    log.info("同步任务结束")


if __name__ == "__main__":
    main()
