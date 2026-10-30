#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各个上游开源软件 release 里的安装包，自动同步成当前仓库的一个 Release。

流程：
  1. 读 .github/sync-config.json，逐个上游仓库查最新 release（可跟随预发布）；
  2. 按配置里每个「平台目标」的正则挑出安装包（保持上游原始文件名）；
  3. 和 .github/sync-state.json 比对，统计有几个软件出现了新版本；
  4. 达到阈值（默认 3 个上游有更新）才发：草稿 Release -> 逐个下载+上传
     -> 生成 SHA256SUMS -> 发布；
  5. 更新 state 文件（由 workflow 负责提交）。

Release 正文用 HTML 表格 + shields.io 徽章展示，每个软件一张「OS / Download」表。

只依赖 Python 标准库 + GitHub CLI(gh)；gh 仅用于 create/upload/edit/delete Release。

用法示例：
  python3 scripts/sync_upstreams.py --dry-run            # 只看计划，不下载不上传
  python3 scripts/sync_upstreams.py --force              # 忽略更新数量判断，直接发
  python3 scripts/sync_upstreams.py --only flclash       # 只把指定软件计入"有更新"的统计
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

API = "https://api.github.com"
USER_AGENT = "SiverKing-VPN_Airport-mirror/1.0 (+https://github.com/SiverKing/VPN_Airport)"
BADGE_BASE = "https://img.shields.io/badge/"

# Release 正文里表格的行顺序
OS_ORDER = ("android", "windows", "macos", "ios", "linux")
# 转置表格的表头顺序（一行一个软件，列是平台，最省纵向空间）
TABLE_OS = ("windows", "macos", "ios", "android", "linux")
OS_LABELS = {"android": "Android", "windows": "Windows", "macos": "macOS", "ios": "iOS", "linux": "Linux"}

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / ".github" / "sync-config.json"
DEFAULT_STATE = REPO_ROOT / ".github" / "sync-state.json"


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class SyncError(RuntimeError):
    """可预期的失败：打日志后以非零退出码结束。"""


class ConfigError(SyncError):
    """配置文件写错了。"""


class ApiError(SyncError):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"HTTP {status} {url} :: {message}")
        self.status = status
        self.url = url


# --------------------------------------------------------------------------- #
# HTTP / GitHub API
# --------------------------------------------------------------------------- #
def _http(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: bytes | None = None,
    accept: str = "application/vnd.github+json",
    timeout: int = 120,
    attempts: int = 3,
) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(2 * attempt)
                last_error = ApiError(exc.code, url, body)
                continue
            raise ApiError(exc.code, url, body) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
    raise SyncError(f"请求失败 {url}: {last_error}")


def api_json(path: str, token: str | None, *, method: str = "GET", data: dict | None = None):
    url = path if path.startswith("http") else API + path
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    raw = _http(url, token=token, method=method, payload=payload)
    if not raw:
        return None
    return json.loads(raw)


def resolve_release(token: str | None, repo: str, include_prerelease: bool) -> tuple[dict, list[str]]:
    """取上游"最新"release，返回 (release, 附加提示列表)。

    - 默认（只跟稳定版）：用 /releases/latest，它天然跳过 prerelease/draft，
      而且有些项目（如 Karing）最近 N 个全是 prerelease，用列表分页反而找不到稳定版；
    - include_prerelease=True：看 releases 列表，取第一个非 draft（含 prerelease）。
    """
    if not include_prerelease:
        try:
            latest = api_json(f"/repos/{repo}/releases/latest", token)
            if isinstance(latest, dict) and latest.get("tag_name") and not latest.get("prerelease"):
                return latest, []
        except ApiError as exc:
            if exc.status != 404:
                raise

    listed = api_json(f"/repos/{repo}/releases?per_page=15", token) or []
    if isinstance(listed, dict):
        listed = [listed]
    for candidate in listed:
        if not isinstance(candidate, dict) or not candidate.get("tag_name"):
            continue
        if candidate.get("draft"):
            continue
        if candidate.get("prerelease") and not include_prerelease:
            continue
        return candidate, []
    raise SyncError(f"{repo}: 找不到可用的 release（可能全是 draft，或全是 prerelease）")


def list_tags(token: str | None, repo: str) -> list[str]:
    tags: list[str] = []
    page = 1
    while page <= 3:  # 最多看 300 个 tag，够用
        batch = api_json(f"/repos/{repo}/tags?per_page=100&page={page}", token) or []
        if not isinstance(batch, list) or not batch:
            break
        tags.extend(item["name"] for item in batch if isinstance(item, dict) and item.get("name"))
        if len(batch) < 100:
            break
        page += 1
    return tags


def latest_releases(token: str | None, repo: str, count: int = 10) -> list[dict]:
    data = api_json(f"/repos/{repo}/releases?per_page={count}", token) or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
# 配置 / 状态
# --------------------------------------------------------------------------- #
def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        # utf-8-sig：兼容 Windows 编辑器保存时带的 BOM
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON: {exc}") from exc


def load_config(path: Path) -> dict:
    config = load_json(path)
    if not isinstance(config, dict):
        raise ConfigError(f"找不到配置 {path}")
    apps = config.get("apps")
    if not isinstance(apps, list) or not apps:
        raise ConfigError("配置里 apps 不能为空")

    seen: set[str] = set()
    for app in apps:
        for field in ("key", "name", "repo", "targets"):
            if not app.get(field):
                raise ConfigError(f"apps 里的每一项都需要 {field} 字段: {app}")
        if app["key"] in seen:
            raise ConfigError(f"重复的 app key: {app['key']}")
        seen.add(app["key"])
        targets = app["targets"]
        if not isinstance(targets, list) or not targets:
            raise ConfigError(f"{app['key']}: targets 需要是非空数组")
        target_keys: set[str] = set()
        for target in targets:
            for field in ("key", "os", "badge"):
                if not target.get(field):
                    raise ConfigError(f"{app['key']}: 每个 target 都需要 {field} 字段: {target}")
            # 目标分两种：可下载的（match 正则）和跳转链接（store_url，例如 App Store）
            if not target.get("match") and not target.get("store_url"):
                raise ConfigError(
                    f"{app['key']}/{target['key']}: 需要 match（下载正则）或 store_url（跳转链接）之一"
                )
            if target.get("store_url") and target["os"] in {item for item in OS_ORDER if item != "ios"}:
                raise ConfigError(f"{app['key']}/{target['key']}: store_url 目前只用于 ios 平台")
            if target["os"] not in OS_LABELS:
                raise ConfigError(f"{app['key']}: 未知平台 {target['os']}（只支持 {'/'.join(OS_ORDER)}）")
            if target["key"] in target_keys:
                raise ConfigError(f"{app['key']}: 重复的 target key {target['key']}")
            target_keys.add(target["key"])
        if app.get("version_from_tag") and not isinstance(app["version_from_tag"], str):
            raise ConfigError(f"{app['key']}: version_from_tag 需要是字符串正则")

    config.setdefault("settings", {})
    return config


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 选择 / 比对
# --------------------------------------------------------------------------- #
def _as_patterns(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    raise ConfigError(f"正则配置格式不对: {value!r}")


def pick_targets(app: dict, release: dict, excludes: list[str]) -> dict[str, dict]:
    """按每个平台目标的正则挑资产；同一个目标匹配到多个直接报错，避免悄悄选错。

    只有带 match 的「可下载目标」参与挑包；store_url 目标（如 App Store）不下载任何文件。
    """
    assets = [item for item in (release.get("assets") or []) if isinstance(item, dict) and item.get("name")]
    app_excludes = [str(pattern) for pattern in (app.get("exclude") or [])]
    selected: dict[str, dict] = {}
    for target in app["targets"]:
        if not target.get("match"):
            continue
        patterns = _as_patterns(target["match"])
        matches = []
        for asset in assets:
            name = asset["name"]
            if any(re.search(pattern, name) for pattern in excludes + app_excludes):
                continue
            if any(re.fullmatch(pattern, name) for pattern in patterns):
                matches.append(asset)
        if len(matches) > 1:
            names = ", ".join(sorted(item["name"] for item in matches))
            raise ConfigError(
                f"{app['key']}/{target['key']}: 正则匹配到多个文件，请改得更精确: {names}"
            )
        if matches:
            selected[target["key"]] = matches[0]
    return selected


def fingerprint(asset: dict) -> dict:
    digest = (asset.get("digest") or "")
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:"):]
    return {
        "name": asset["name"],
        "size": int(asset.get("size") or 0),
        "sha256": digest,
    }


def diff_app(previous: dict | None, tag: str, selected: dict[str, dict]) -> list[str]:
    """返回变化原因列表；空列表 = 没有变化。缺失的目标不算变化（上游偶尔缺包）。"""
    if not previous:
        return ["首次同步"]
    reasons: list[str] = []
    if previous.get("tag") != tag:
        reasons.append(f"{previous.get('tag') or '?'} → {tag}")
    previous_targets = previous.get("targets") or {}
    for key, asset in selected.items():
        old = previous_targets.get(key)
        new = fingerprint(asset)
        if not old:
            reasons.append(f"{key} 新增")
            continue
        if old.get("name") != new["name"]:
            # 典型情况：上游把版本号从文件名里去掉了
            if reasons and "→" in reasons[0]:
                reasons.append(f"{key} 文件名变化")
            else:
                reasons.append(f"{key} 文件名变化 {old.get('name')} → {new['name']}")
        elif old.get("size") not in (None, new["size"]):
            reasons.append(f"{key} 体积变化")
        elif old.get("sha256") and new["sha256"] and old["sha256"] != new["sha256"]:
            reasons.append(f"{key} 校验值变化")
    return reasons


def strip_version_prefix(tag: str) -> str:
    return re.sub(r"^[vV]", "", tag.strip())


def display_version(tag: str, app: dict) -> str:
    """展示用版本号；app.version_from_tag 可以先从 tag 里截出干净的版本（如 1.0.31.1703 -> 1.0.31）。"""
    pattern = app.get("version_from_tag")
    if pattern:
        match = re.search(pattern, tag)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return strip_version_prefix(tag)


# --------------------------------------------------------------------------- #
# 生成 Release 正文（HTML 表格 + shields.io 徽章，照抄上游 FlClash 的样式）
# --------------------------------------------------------------------------- #
def render_body(
    tag: str,
    repo: str,
    entries: list[dict],
    source_order: list[str],
    *,
    checksums: bool,
) -> str:
    """一张转置的表格：表头是各平台，一行一个软件，徽章横向排列（最省纵向空间）。"""
    base = f"https://github.com/{repo}/releases/download/{tag}"
    updated = [f"{entry['name']} {entry['version']}" for entry in entries if entry.get("reasons")]
    unchanged = [f"{entry['name']} {entry['version']}" for entry in entries if not entry.get("reasons")]
    lines = [
        "# 软件备份",
        "",
        "> 点图标直接下载（全部为上游原版安装包，保持原始文件名）；点软件名看上游更新日志。",
    ]
    if updated:
        lines.append(
            f"> **本次更新（{len(updated)} 个）**：" + " · ".join(f"**{item}**" for item in updated)
        )
    if unchanged:
        lines.append(f"> 版本未变（{len(unchanged)} 个，和上一个 Release 相同）：" + " · ".join(unchanged))
    if not updated:
        lines.append("> 本次没有检测到上游新版本（强制/兜底发布），全部沿用上一个 Release 的版本。")
    lines += [
        "",
        "## 直链下载",
        "",
        '<div align=left>',
        '<table>',
        '    <thead align=left>',
        '        <tr><th>软件</th>'
        + "".join(f"<th>{OS_LABELS[os_key]}</th>" for os_key in TABLE_OS)
        + "</tr>",
        '    </thead>',
        '    <tbody align=left>',
    ]

    for entry in entries:
        targets_by_os: dict[str, list[dict]] = {}
        for target in entry["targets"]:
            targets_by_os.setdefault(target["os"], []).append(target)
        name_url = entry.get("html_url") or f"https://github.com/{entry['repo']}/releases"
        mark = "<b>[新]</b> " if entry.get("reasons") else ""
        cells = []
        for os_key in TABLE_OS:
            badges = []
            for target in targets_by_os.get(os_key, []):
                if target.get("store_url"):
                    # iOS 这类目标不放直链，只放商店跳转
                    badges.append(
                        f'<a href="{target["store_url"]}"><img src="{BADGE_BASE}{target["badge"]}" alt="App Store"></a>'
                    )
                elif target.get("name"):
                    badges.append(
                        f'<a href="{base}/{target["name"]}"><img src="{BADGE_BASE}{target["badge"]}"></a>'
                    )
            cells.append("<td>" + "&#160;".join(badges) + "</td>")
        lines.append(
            f'        <tr><td>{mark}<a href="{name_url}">{entry["name"]} {entry["version"]}</a></td>'
            + "".join(cells)
            + "</tr>"
        )

    lines.extend(['    </tbody>', '</table>', '</div>', ""])

    notes = []
    for entry in entries:
        missing_os = sorted(entry.get("missing_os") or [], key=OS_ORDER.index)
        if missing_os:
            notes.append(
                f"> {entry['name']}：本次上游未提供 "
                + "、".join(OS_LABELS[item] for item in missing_os)
                + " 安装包"
            )
    if notes:
        lines.extend(notes)
        lines.append("")

    lines.append("## 校验")
    lines.append("")
    if checksums:
        lines.append(f"[SHA256SUMS.txt]({base}/SHA256SUMS.txt)（每个安装包的 SHA256 校验值）")
    else:
        lines.append("未生成校验文件。")
    lines.append("")
    lines.append("## 原始链接")
    lines.append("")
    by_key = {entry["key"]: entry for entry in entries}
    ordered = [key for key in source_order if key in by_key]
    ordered += [key for key in by_key if key not in ordered]
    for key in ordered:
        lines.append(f"- https://github.com/{by_key[key]['repo']}/releases")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# 下载 / 校验 / gh
# --------------------------------------------------------------------------- #
class Downloader:
    """下载上游资产。

    优先走 API 端点（带 token，限额更高、更稳），失败再退回浏览器直链。
    """

    def __init__(self, token: str | None) -> None:
        self.token = token

    def _candidates(self, asset: dict) -> list[tuple[str, dict]]:
        urls: list[tuple[str, dict]] = []
        if self.token and asset.get("url"):
            urls.append((
                f"{asset['url']}",
                {"Accept": "application/octet-stream", "Authorization": f"Bearer {self.token}"},
            ))
        if asset.get("browser_download_url"):
            urls.append((
                f"{asset['browser_download_url']}",
                {"Accept": "application/octet-stream"},
            ))
        if not urls:
            raise SyncError(f"{asset.get('name')}: 缺少下载地址")
        return urls

    def fetch(self, asset: dict, target: Path, attempts: int = 4) -> str:
        expected_size = int(asset.get("size") or 0)
        expected_digest = (asset.get("digest") or "")
        if expected_digest.startswith("sha256:"):
            expected_digest = expected_digest[len("sha256:"):]

        last_error: Exception | None = None
        candidates = self._candidates(asset)
        for attempt in range(1, attempts + 1):
            url, extra_headers = candidates[(attempt - 1) % len(candidates)]
            digest = hashlib.sha256()
            try:
                headers = {"User-Agent": USER_AGENT, **extra_headers}
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=600) as response, target.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        digest.update(chunk)
                got = target.stat().st_size
                if expected_size and got != expected_size:
                    raise SyncError(f"体积不符：期望 {expected_size}，实际 {got}")
                actual = digest.hexdigest()
                if expected_digest and actual != expected_digest:
                    raise SyncError(f"SHA256 不符：期望 {expected_digest}，实际 {actual}")
                return actual
            except urllib.error.HTTPError as exc:
                last_error = exc
                wait = 60 if exc.code == 429 else 5 * attempt
                target.unlink(missing_ok=True)
                if attempt == attempts:
                    break
                print(f"    ! 第 {attempt} 次下载失败（HTTP {exc.code}），{wait}s 后重试…", file=sys.stderr)
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001 - 网络/校验错误都重试
                last_error = exc
                target.unlink(missing_ok=True)
                if attempt == attempts:
                    break
                print(f"    ! 第 {attempt} 次下载失败（{exc}），重试…", file=sys.stderr)
                time.sleep(5 * attempt)
        raise SyncError(f"下载 {asset.get('name')} 失败: {last_error}")


def gh(args: list[str], token: str) -> str:
    if shutil.which("gh") is None:
        raise SyncError("找不到 gh（GitHub CLI）。Actions 的 ubuntu-latest 自带 gh，本地调试请先安装。")
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env["GH_HOST"] = "github.com"
    env["NO_COLOR"] = "1"
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SyncError(f"gh {' '.join(args)} 失败: {(proc.stderr or proc.stdout).strip()[:600]}")
    return proc.stdout


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def resolve_token(explicit: str | None) -> str | None:
    return explicit or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def now_in(name: str) -> datetime:
    if ZoneInfo is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.now(ZoneInfo(name))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def choose_tag(now: datetime, existing: set[str], prefix: str) -> str:
    base = f"{prefix}{now:%Y%m%d}"
    if base not in existing:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}{suffix}"
        if candidate not in existing:
            return candidate
    for index in range(2, 100):
        candidate = f"{base}-{index}"
        if candidate not in existing:
            return candidate
    raise SyncError("同一天标签用尽，请检查 tag 列表")


def write_summary(markdown: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError:
        pass


def days_since(release: dict, now: datetime) -> float | None:
    stamp = release.get("created_at") or release.get("published_at")
    if not stamp:
        return None
    made = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (now.astimezone(timezone.utc) - made).total_seconds() / 86400


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把上游安装包同步成当前仓库的 Release")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="当前仓库 owner/name")
    parser.add_argument("--token", default=None, help="默认读 GH_TOKEN / GITHUB_TOKEN")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不下载不上传")
    parser.add_argument("--force", action="store_true", help="忽略更新数量阈值，直接发一个 Release")
    parser.add_argument("--only", default=None, help="逗号分隔的 app key，只把这些软件计入『有更新』的统计")
    parser.add_argument(
        "--tag",
        default=None,
        help="覆盖要生成的 tag（仅用于重新生成/预览某个已有 Release 的正文，需配合 --dry-run）",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    settings = config.get("settings") or {}
    state = load_json(args.state, default=None) or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema", 2)
    if not isinstance(state.get("apps"), dict):
        state["apps"] = {}

    token = resolve_token(args.token)
    repo = args.repo
    if not repo:
        raise SyncError("缺少 --repo（或环境变量 GITHUB_REPOSITORY）")

    only = {item.strip() for item in (args.only or "").split(",") if item.strip()}
    if only:
        unknown = only - {app["key"] for app in config["apps"]}
        if unknown:
            raise ConfigError(f"--only 里有未知的 app key: {', '.join(sorted(unknown))}")
    excludes = [str(item) for item in (settings.get("asset_excludes") or [])]
    default_prerelease = bool(settings.get("include_prerelease"))
    prefix = settings.get("tag_prefix", "v")
    checksums = bool(settings.get("checksums", True))
    min_apps = int(settings.get("min_updated_apps_to_release") or 0)
    min_days = int(settings.get("min_days_between_releases") or 0)
    max_days = int(settings.get("max_days_between_releases") or 0)

    # 1) 逐个上游解析最新 release，挑包，比对
    entries: list[dict] = []
    updated: list[dict] = []
    report: list[str] = []
    for app in config["apps"]:
        key, name, upstream = app["key"], app["name"], app["repo"]
        check_this = not only or key in only
        app_prerelease = bool(app.get("include_prerelease", default_prerelease))
        release, skipped = resolve_release(token, upstream, app_prerelease)
        tag = str(release["tag_name"]).strip()
        if skipped:
            print(f"  ! {name}: 跳过 {len(skipped)} 个预发布版本（{', '.join(skipped[:3])}），使用 {tag}")
        selected = pick_targets(app, release, excludes)
        download_targets = [target for target in app["targets"] if target.get("match")]
        store_targets = [target for target in app["targets"] if target.get("store_url")]
        if not selected and not store_targets:
            actual = sorted(
                item["name"] for item in (release.get("assets") or []) if isinstance(item, dict) and item.get("name")
            )
            raise SyncError(
                f"{upstream}@{tag}: 没有任何资产匹配配置里的正则。\n"
                f"  上游当前资产: {', '.join(actual[:20])}{' …' if len(actual) > 20 else ''}\n"
                f"  请更新 .github/sync-config.json 里 {key} 的 match 正则"
            )
        missing_targets = [
            target for target in download_targets if target["key"] not in selected
        ]
        missing = [target["key"] for target in missing_targets]
        missing_os = sorted({target["os"] for target in missing_targets}, key=OS_ORDER.index)
        if missing:
            print(f"  ! {name}@{tag}: 缺少 {', '.join(missing)}（本次跳过这些平台）")
        reasons = diff_app(state["apps"].get(key), tag, selected)
        targets_display = [
            {**target, "name": selected[target["key"]]["name"]}
            for target in download_targets
            if target["key"] in selected
        ] + [dict(target) for target in store_targets]
        entry = {
            "key": key,
            "name": name,
            "repo": upstream,
            "tag": tag,
            "version": display_version(tag, app),
            "html_url": release.get("html_url")
            or f"https://github.com/{upstream}/releases/tag/{tag}",
            "targets": targets_display,
            "assets": {
                selected[target["key"]]["name"]: selected[target["key"]]
                for target in download_targets
                if target["key"] in selected
            },
            "missing": missing,
            "missing_os": missing_os,
            "reasons": reasons,
        }
        entries.append(entry)
        if reasons and check_this:
            updated.append(entry)
        detail = ", ".join(
            f"{target['key']}={selected[target['key']]['name']}"
            for target in download_targets
            if target["key"] in selected
        )
        if store_targets:
            detail = (detail + "  " if detail else "") + f"商店链接={len(store_targets)} 个"
        print(f"  {name:<12} {tag:<10} {len(selected)} 个文件  {detail}")
        report.append(
            f"{'**有更新**' if reasons and check_this else '无变化'}  \u00b7  {name} `{tag}`"
            + (f"：{'; '.join(reasons)}" if reasons else "")
        )

    if not entries:
        raise SyncError("配置里没有可处理的 app")

    print("")
    for line in report:
        print(line)

    # 2) 本次要不要发 Release
    own_releases = latest_releases(token, repo, 5)
    last_published = next((item for item in own_releases if not item.get("draft")), None)
    now = now_in(settings.get("timezone") or "Asia/Shanghai")
    elapsed = days_since(last_published, now) if last_published else None

    decision, why = decide_release(
        updated=updated,
        force=args.force,
        min_apps=min_apps,
        min_days=min_days,
        max_days=max_days,
        elapsed_days=elapsed,
        last_tag=(last_published or {}).get("tag_name"),
    )
    print(f"\n发布判断: {decision} —— {why}")
    if decision != "publish":
        write_summary(f"## 上游同步\n\n{decision}：{why}\n")
        return 0

    # 3) 计算标签和正文
    existing_tags = set(list_tags(token, repo))
    # 草稿 Release 的 tag 还没真正建出来（pending），也要算进去，避免同日重跑撞标签
    existing_tags.update(
        str(item["tag_name"]) for item in own_releases if item.get("tag_name")
    )
    tag = args.tag or choose_tag(now, existing_tags, prefix)
    body = render_body(
        tag,
        repo,
        entries,
        [str(item) for item in (config.get("source_order") or [])],
        checksums=checksums,
    )
    total_files = sum(len(entry["assets"]) for entry in entries)
    total_bytes = sum(int(asset.get("size") or 0) for entry in entries for asset in entry["assets"].values())
    print(f"\n准备发布 {tag}，共 {total_files} 个文件，约 {total_bytes / 1073741824:.2f} GB。")
    print("=" * 70)
    print(body)
    print("=" * 70)

    write_summary(
        "## 上游同步\n\n"
        + f"发布判断：**{decision}** —— {why}\n\n"
        + f"计划标签：`{tag}`，{total_files} 个文件\n\n"
        + "\n".join(f"- {line}" for line in report)
        + "\n"
    )

    if args.dry_run:
        print("dry-run：不创建 Release、不下载文件。")
        return 0

    if not token:
        raise SyncError("发布需要 token：请设置 GH_TOKEN 或 GITHUB_TOKEN")

    # 4) 建草稿 -> 下载上传 -> 发布
    created = False
    published = False
    workdir = Path(tempfile.mkdtemp(prefix="sync-upstreams-"))
    files = [
        (entry, target, entry["assets"][target["name"]])
        for entry in entries
        for target in entry["targets"]
        if target.get("name") and target["name"] in entry["assets"]
    ]
    try:
        body_file = workdir / "release-body.md"
        body_file.write_text(body, encoding="utf-8")
        print(f"\n[1/4] 创建草稿 Release {tag}")
        gh(["release", "create", tag, "--repo", repo, "--draft", "--title", tag,
            "--notes-file", str(body_file)], token)
        created = True

        downloader = Downloader(token)
        checksums_lines: list[str] = []
        for index, (entry, target, remote) in enumerate(files, start=1):
            name = target["name"]
            path = workdir / name
            print(f"[2/4] ({index}/{len(files)}) {entry['name']} 下载 {name}")
            sha256 = downloader.fetch(remote, path)
            checksums_lines.append(f"{sha256}  {name}")
            print(f"      上传 {name} ({path.stat().st_size / 1024 / 1024:.1f} MB)")
            gh(["release", "upload", tag, str(path), "--repo", repo, "--clobber"], token)
            path.unlink(missing_ok=True)

        if checksums:
            checksum_file = workdir / "SHA256SUMS.txt"
            checksum_file.write_text("\n".join(sorted(checksums_lines)) + "\n", encoding="utf-8")
            print("[3/4] 上传 SHA256SUMS.txt")
            gh(["release", "upload", tag, str(checksum_file), "--repo", repo, "--clobber"], token)

        print(f"[4/4] 发布 Release {tag}")
        gh(["release", "edit", tag, "--repo", repo, "--draft=false"], token)
        published = True
    finally:
        if created and not published:
            print(f"\n出错，清理草稿 Release {tag} …", file=sys.stderr)
            try:
                gh(["release", "delete", tag, "--repo", repo, "--yes"], token)
            except Exception as exc:  # noqa: BLE001
                print(f"清理失败，请手动删除：{exc}", file=sys.stderr)
        shutil.rmtree(workdir, ignore_errors=True)

    # 5) 更新 state
    state["schema"] = 2
    state["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state["last_release_tag"] = tag
    for entry in entries:
        previous_targets = ((state["apps"].get(entry["key"]) or {}).get("targets")) or {}
        merged = dict(previous_targets)
        merged.update({
            target["key"]: fingerprint(entry["assets"][target["name"]])
            for target in entry["targets"]
            if target.get("name") and target["name"] in entry["assets"]
        })
        state["apps"][entry["key"]] = {
            "tag": entry["tag"],
            "repo": entry["repo"],
            "targets": merged,
        }
    save_state(args.state, state)

    print(f"\n✅ 已发布 {tag}: https://github.com/{repo}/releases/tag/{tag}")
    write_summary(
        f"\n✅ 已发布 [`{tag}`](https://github.com/{repo}/releases/tag/{tag})，{total_files} 个安装包。\n"
    )
    return 0


def decide_release(
    *,
    updated: list[dict],
    force: bool,
    min_apps: int,
    min_days: int,
    max_days: int,
    elapsed_days: float | None,
    last_tag: str | None,
) -> tuple[str, str]:
    """返回 (publish|skip, 原因)。"""
    if force:
        return "publish", "手动强制发布"
    if not updated:
        return "skip", "没有新版本"
    if min_days > 0 and elapsed_days is not None and elapsed_days < min_days:
        return "skip", f"距上次发布（{last_tag}）不足 {min_days} 天"
    if len(updated) >= max(min_apps, 1):
        return "publish", f"{len(updated)} 个上游有更新（阈值 {min_apps}）"
    if max_days > 0 and elapsed_days is not None and elapsed_days >= max_days:
        return "publish", f"距上次发布（{last_tag}）已 {int(elapsed_days)} 天，兜底发布"
    return "skip", f"只有 {len(updated)} 个上游有更新（需要 ≥{min_apps} 个）"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        write_summary(f"## 上游同步\n\n❌ 失败：{exc}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
