"""集中式配置管理模块。

支持四层配置源，优先级从高到低：
1. 命令行参数 (CLI)
2. YAML 配置文件
3. 环境变量
4. 内置默认值

配置文件查找顺序：
1. --config 指定路径
2. ./config.yaml
3. ./nano-search-mcp.yaml
4. ~/.config/nano-search-mcp/config.yaml
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── 默认的 deferred-tasks.md 路径（相对于项目根） ──────────────
_DEFAULT_DEFERRED_TASKS_PATH = str(
    Path(__file__).parent.parent.parent / "docs" / "source-intake" / "deferred-tasks.md"
)


# ── 数据类定义 ───────────────────────────────────────────────


@dataclass
class ApiSettings:
    """百炼 API 相关配置。"""

    dashscope_api_key: str = ""
    bailian_websearch_endpoint: str = (
        "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"
    )
    bailian_mcp_timeout: float = 30.0


@dataclass
class ServerSettings:
    """MCP Server 运行参数。"""

    transport: str = "streamable-http"
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class HttpSettings:
    """通用 HTTP 请求参数（重试、限流）。"""

    max_retries: int = 3
    backoff_base: float = 2.0
    request_interval: float = 1.0


@dataclass
class CacheSettings:
    """缓存配置。"""

    cache_dir: str = "~/.cache/nano_search_mcp"
    list_cache_ttl: int = 3600
    detail_cache_ttl: int = 604800


@dataclass
class FetchSettings:
    """Playwright 页面抓取参数。"""

    playwright_wait_ms: int = 2000
    max_content_length: int = 500_000


@dataclass
class AnnouncementsSettings:
    """临时公告模块参数。"""

    max_pages: int = 10


@dataclass
class IndustryReportsSettings:
    """行业研报模块参数。"""

    max_pages: int = 5


@dataclass
class IrMeetingsSettings:
    """投资者关系会议模块参数。"""

    max_pages: int = 20


@dataclass
class DeferredSearchSettings:
    """延迟搜索模块参数。"""

    deferred_tasks_path: str = _DEFAULT_DEFERRED_TASKS_PATH


@dataclass
class IndustryPoliciesSettings:
    """行业政策模块参数。"""

    max_per_query: int = 10
    top_n: int = 5


@dataclass
class Settings:
    """顶层配置，聚合所有子配置。"""

    api: ApiSettings = field(default_factory=ApiSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    fetch: FetchSettings = field(default_factory=FetchSettings)
    announcements: AnnouncementsSettings = field(default_factory=AnnouncementsSettings)
    industry_reports: IndustryReportsSettings = field(
        default_factory=IndustryReportsSettings
    )
    ir_meetings: IrMeetingsSettings = field(default_factory=IrMeetingsSettings)
    deferred_search: DeferredSearchSettings = field(
        default_factory=DeferredSearchSettings
    )
    industry_policies: IndustryPoliciesSettings = field(
        default_factory=IndustryPoliciesSettings
    )

    @property
    def _cache_root(self) -> Path:
        return Path(self.cache.cache_dir).expanduser()

    @property
    def announcements_cache_dir(self) -> Path:
        return self._cache_root / "announcements"

    @property
    def industry_reports_cache_dir(self) -> Path:
        return self._cache_root / "industry_reports"

    @property
    def penalties_cache_dir(self) -> Path:
        return self._cache_root / "penalties"

    @property
    def ir_meetings_cache_dir(self) -> Path:
        return self._cache_root / "ir_meetings"


# ── 旧环境变量 → 新配置路径映射（向后兼容） ─────────────────────
_LEGACY_ENV_MAP: dict[str, tuple[str, str]] = {
    "DASHSCOPE_API_KEY": ("api", "dashscope_api_key"),
    "BAILIAN_WEBSEARCH_ENDPOINT": ("api", "bailian_websearch_endpoint"),
    "BAILIAN_MCP_TIMEOUT": ("api", "bailian_mcp_timeout"),
}

# ── 新环境变量前缀 ────────────────────────────────────────────
_ENV_PREFIX = "NANO_SEARCH_MCP"

# ── 子配置类注册表 ────────────────────────────────────────────
_SECTION_CLASSES: dict[str, type] = {
    "api": ApiSettings,
    "server": ServerSettings,
    "http": HttpSettings,
    "cache": CacheSettings,
    "fetch": FetchSettings,
    "announcements": AnnouncementsSettings,
    "industry_reports": IndustryReportsSettings,
    "ir_meetings": IrMeetingsSettings,
    "deferred_search": DeferredSearchSettings,
    "industry_policies": IndustryPoliciesSettings,
}


# ── 工具函数 ─────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个字典，override 覆盖 base。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _cast_value(value: str, target_type: type) -> Any:
    """将环境变量字符串值转换为目标类型。"""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _find_config_file(cli_path: str | None = None) -> Path | None:
    """按优先级查找配置文件。"""
    candidates: list[Path] = []

    if cli_path:
        p = Path(cli_path).expanduser()
        if p.is_file():
            return p
        logger.warning("--config 指定的配置文件不存在: %s", cli_path)
        return None

    # 当前工作目录
    cwd = Path.cwd()
    candidates.append(cwd / "config.yaml")
    candidates.append(cwd / "nano-search-mcp.yaml")

    # XDG 用户配置目录
    candidates.append(
        Path("~/.config/nano-search-mcp/config.yaml").expanduser()
    )

    for path in candidates:
        if path.is_file():
            logger.info("使用配置文件: %s", path)
            return path

    return None


def _load_yaml_config(path: Path) -> dict[str, Any]:
    """加载 YAML 配置文件。"""
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            logger.warning("配置文件内容不是字典格式，已忽略: %s", path)
            return {}
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载配置文件失败: %s — %s", path, exc)
        return {}


def _load_env_overrides() -> dict[str, Any]:
    """从环境变量读取配置覆盖。

    支持两种命名格式：
    1. 旧格式（向后兼容）：DASHSCOPE_API_KEY, BAILIAN_WEBSEARCH_ENDPOINT, BAILIAN_MCP_TIMEOUT
    2. 新格式：NANO_SEARCH_MCP_{SECTION}_{FIELD}（大写，下划线分隔）
    """
    overrides: dict[str, Any] = {}

    # 1) 旧环境变量兼容
    for env_name, (section, field_name) in _LEGACY_ENV_MAP.items():
        value = os.environ.get(env_name)
        if value is not None:
            overrides.setdefault(section, {})[field_name] = value

    # 2) 新环境变量（NANO_SEARCH_MCP_{SECTION}_{FIELD}）
    prefix = f"{_ENV_PREFIX}_"
    for env_name, env_value in os.environ.items():
        if not env_name.startswith(prefix):
            continue

        rest = env_name[len(prefix):].lower()
        # 尝试匹配 section_field 模式
        matched = False
        for section_name, cls in _SECTION_CLASSES.items():
            section_prefix = section_name + "_"
            if rest.startswith(section_prefix):
                field_name = rest[len(section_prefix):]
                # 验证字段存在
                field_names = {f.name for f in fields(cls)}
                if field_name in field_names:
                    # 获取目标类型进行类型转换
                    target_type = next(
                        f.type for f in fields(cls) if f.name == field_name
                    )
                    try:
                        typed_value = _cast_value(env_value, eval(target_type))  # noqa: S307
                    except (ValueError, TypeError):
                        typed_value = env_value
                    overrides.setdefault(section_name, {})[field_name] = typed_value
                    matched = True
                    break

        if not matched:
            logger.debug("忽略无法映射的环境变量: %s", env_name)

    return overrides


def _apply_cli_overrides(
    data: dict[str, Any],
    cli_args: dict[str, Any] | None,
) -> dict[str, Any]:
    """将 CLI 参数覆盖到配置字典。"""
    if not cli_args:
        return data

    # CLI → 配置路径映射
    cli_mapping: dict[str, tuple[str, str]] = {
        "transport": ("server", "transport"),
        "host": ("server", "host"),
        "port": ("server", "port"),
    }

    for cli_key, (section, field_name) in cli_mapping.items():
        value = cli_args.get(cli_key)
        if value is not None:
            data.setdefault(section, {})[field_name] = value

    return data


def _dict_to_settings(data: dict[str, Any]) -> Settings:
    """将合并后的字典转换为 Settings dataclass 实例。"""
    kwargs: dict[str, Any] = {}
    for section_name, cls in _SECTION_CLASSES.items():
        section_data = data.get(section_name, {})
        if isinstance(section_data, dict):
            # 只保留 dataclass 中定义的字段
            valid_fields = {f.name for f in fields(cls)}
            filtered = {k: v for k, v in section_data.items() if k in valid_fields}
            kwargs[section_name] = cls(**filtered)
        elif isinstance(section_data, cls):
            kwargs[section_name] = section_data
    return Settings(**kwargs)


def _settings_to_defaults() -> dict[str, Any]:
    """从 dataclass 默认值构造字典。"""
    defaults: dict[str, Any] = {}
    default_settings = Settings()
    for section_name, cls in _SECTION_CLASSES.items():
        section_obj = getattr(default_settings, section_name)
        section_dict: dict[str, Any] = {}
        for f in fields(cls):
            section_dict[f.name] = getattr(section_obj, f.name)
        defaults[section_name] = section_dict
    return defaults


# ── 全局单例 ─────────────────────────────────────────────────

_settings: Settings | None = None
_initialized: bool = False


def init_settings(
    cli_args: dict[str, Any] | None = None,
    config_path: str | None = None,
) -> Settings:
    """显式初始化全局配置单例。

    执行四层合并：defaults → env → YAML → CLI。
    应在 server.main() 中 argparse 之后调用。
    """
    global _settings, _initialized

    # 1) 默认值
    data = _settings_to_defaults()

    # 2) 环境变量
    env_overrides = _load_env_overrides()
    if env_overrides:
        data = _deep_merge(data, env_overrides)

    # 3) YAML 配置文件
    yaml_path = _find_config_file(config_path)
    if yaml_path is not None:
        yaml_data = _load_yaml_config(yaml_path)
        data = _deep_merge(data, yaml_data)

    # 4) CLI 参数
    data = _apply_cli_overrides(data, cli_args)

    _settings = _dict_to_settings(data)
    _initialized = True
    return _settings


def get_settings() -> Settings:
    """获取全局配置单例。

    若未通过 init_settings() 初始化，则 fallback 到默认值 + 环境变量 + YAML。
    """
    global _settings, _initialized
    if _settings is not None:
        return _settings

    # fallback: defaults → env → YAML（兼容 api.py import-time 和测试场景）
    data = _settings_to_defaults()
    env_overrides = _load_env_overrides()
    if env_overrides:
        data = _deep_merge(data, env_overrides)
    yaml_path = _find_config_file()
    if yaml_path is not None:
        yaml_data = _load_yaml_config(yaml_path)
        data = _deep_merge(data, yaml_data)
    _settings = _dict_to_settings(data)
    return _settings


def _reset_settings() -> None:
    """重置全局单例（仅供测试使用）。"""
    global _settings, _initialized
    _settings = None
    _initialized = False


# ── 示例配置生成 ─────────────────────────────────────────────

_SAMPLE_CONFIG = """\
# NanoSearch MCP Server 配置文件
# 配置优先级: CLI 参数 > 本文件 > 环境变量 > 内置默认值
# 文件查找顺序: --config 指定路径 > ./config.yaml > ./nano-search-mcp.yaml > ~/.config/nano-search-mcp/config.yaml

api:
  # 百炼 API 密钥（推荐通过环境变量 DASHSCOPE_API_KEY 设置）
  # dashscope_api_key: "sk-xxx"
  # 百炼 WebSearch MCP 端点 URL
  bailian_websearch_endpoint: "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"
  # 百炼 MCP HTTP 请求超时（秒）
  bailian_mcp_timeout: 30.0

server:
  # MCP transport 类型: streamable-http 或 stdio
  transport: "streamable-http"
  # HTTP 监听地址
  host: "0.0.0.0"
  # HTTP 监听端口
  port: 8000

http:
  # 网络请求最大重试次数
  max_retries: 3
  # 指数退避基数（秒）
  backoff_base: 2.0
  # 相邻请求最小间隔（秒）
  request_interval: 1.0

cache:
  # 缓存根目录（支持 ~ 展开）
  cache_dir: "~/.cache/nano_search_mcp"
  # 列表页缓存 TTL（秒）
  list_cache_ttl: 3600
  # 详情页缓存 TTL（秒）
  detail_cache_ttl: 604800

fetch:
  # Playwright 页面渲染后额外等待时间（毫秒）
  playwright_wait_ms: 2000
  # 返回正文最大字符数（超出截断）
  max_content_length: 500000

announcements:
  # 单次列表抓取最多翻页数
  max_pages: 10

industry_reports:
  # 单次列表抓取最多翻页数
  max_pages: 5

ir_meetings:
  # 单次列表抓取最多翻页数
  max_pages: 20

deferred_search:
  # deferred-tasks.md 文件路径
  # deferred_tasks_path: "docs/source-intake/deferred-tasks.md"

industry_policies:
  # 每条 query 最大返回条数
  max_per_query: 10
  # 最终返回的政策条数上限
  top_n: 5
"""


def generate_sample_config() -> str:
    """返回带注释的示例 YAML 配置文件内容。"""
    return _SAMPLE_CONFIG
