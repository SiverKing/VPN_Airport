### 机场备份  
一元机场:  https://github.com/yiyuanjichang/dizhi  
阿伟云:  https://awyyds.cyou  
魔戒:  https://魔戒.net    (邮箱客服 themojie@pm.me 发送任意消息即可回复 最新可用网址)  
mitce：https://mitce.com/  
BestVPN：https://bitbucket.org/obest/bv

---

## 客户端备份（自动同步）

[Releases](../../releases) 里的客户端安装包由 GitHub Actions 自动从上游采集，**保持上游原始文件名**，
下载链接固定为 `https://github.com/SiverKing/VPN_Airport/releases/download/<tag>/<文件名>`。
Release 正文里每个软件都有一张 `OS / Download` 表格，点图标即可直接下载。

| 软件 | 上游仓库 | 备份的平台 |
| --- | --- | --- |
| FlClash | [chen08209/FlClash](https://github.com/chen08209/FlClash/releases) | Android（arm64 / armv7 / x64）· Windows（安装版 / 便携版）· macOS（Apple Silicon / Intel）· Linux（AppImage / deb / rpm） |
| Clash.Verge | [clash-verge-rev/clash-verge-rev](https://github.com/clash-verge-rev/clash-verge-rev/releases) | Windows x64 · macOS（Apple Silicon / Intel）· Linux（deb / rpm） |
| clash-party | [mihomo-party-org/clash-party](https://github.com/mihomo-party-org/clash-party/releases) | Windows（安装版 / 便携版）· macOS（Apple Silicon / Intel）· Linux（deb / rpm / Arch） |
| v2rayN | [2dust/v2rayN](https://github.com/2dust/v2rayN/releases) | Windows（安装版 / 便携版）· macOS（Apple Silicon / Intel）· Linux（deb / rpm） |
| ClashMeta | [MetaCubeX/ClashMetaForAndroid](https://github.com/MetaCubeX/ClashMetaForAndroid/releases) | Android（arm64 / armv7 / x64） |
| v2rayNG | [2dust/v2rayNG](https://github.com/2dust/v2rayNG/releases) | Android（arm64 / armv7 / x64） |

### 工作原理

- 工作流：`.github/workflows/sync-upstreams.yml`，每天北京时间 **03:23** 检查一次，也可以在 Actions 页面手动触发。
- 脚本：`scripts/sync_upstreams.py`，配置：`.github/sync-config.json`，状态：`.github/sync-state.json`。
- 每次检查会拉各上游最新 Release，按配置里的正则挑包，再和状态文件比对，统计**有几个软件出现了新版本**；
  **≥ 3 个软件有更新时才发一个新 Release**（`min_updated_apps_to_release` 可调），这样不会上游一更新就发一版。
  另外距上次发布超过 30 天时即使更新不足也会兜底发一版（`max_days_between_releases`）。
- 标签沿用 `vYYYYMMDD`，同一天第二次自动加 `a`/`b`。
- 发布采用「草稿 → 全部上传成功 → 转为正式」的流程：中途失败会自动删掉草稿，不会留下残缺的 Release。
- 额外附带 `SHA256SUMS.txt` 校验文件，可用来校验下载到的安装包。

### 维护

- **加/换软件**：改 `.github/sync-config.json` 里的 `apps`（`repo` + `targets` 列表），提交即可。
- **`sync-config.json` 里可用的字段**：

  | 字段 | 位置 | 作用 |
  | --- | --- | --- |
  | `targets[].os` | 每个 app | `android` / `windows` / `macos` / `linux`，决定表格里落在哪一行 |
  | `targets[].match` | 每个 app | 正则，对上游资产名做 fullmatch；同一目标命中多个会直接报错 |
  | `targets[].badge` | 每个 app | shields.io 徽章，如 `Setup-x64-2d7d9a.svg?logo=windows`，换颜色/图标改这里 |
  | `exclude` | 每个 app | 额外排除的正则（如 `win7-`、`fixed_webview2`、`-alpha`） |
  | `include_prerelease` | 每个 app | 是否跟随预发布版。默认不跟（v2rayNG 例外，见下） |
  | `min_updated_apps_to_release` | settings | 多少个软件有更新才发版，默认 3 |
  | `max_days_between_releases` | settings | 兜底天数，默认 30，`0` 为关闭 |
  | `checksums` | settings | 是否额外上传 `SHA256SUMS.txt`，默认开 |

- **注意 v2rayNG**：上游长期把新版本标成 prerelease（2.3.12 / 2.3.13 / 2.3.14 全是），
  只跟正式版会一直停在 2.3.11，所以它的 `include_prerelease` 设为 `true`；只想要正式版就改回 `false`。
- **上游偶尔缺包**：某些版本上游会暂时不提供某个平台的包（例如 v2rayN 7.25.3 没有 macOS 包），
  这种情况只会在 Release 正文里加一行提示，不会中断整次同步。
- **上游改了文件名导致匹配失败**：workflow 会直接标红失败，并在日志里报出是哪个上游、当前有哪些资产；
  改一下 `sync-config.json` 的正则即可。改动可以先用 Actions 的 `dry_run` 输入验证，不产生任何 Release。
- **只想检查某几个软件**：手动触发时填 `only`（如 `flclash,v2rayn`），这次只按这些软件判断要不要发布（发布内容仍是全部软件）。
- **本地预览**（只打印计划，不下载、不发布）：

  ```bash
  GITHUB_REPOSITORY=SiverKing/VPN_Airport python3 scripts/sync_upstreams.py --dry-run
  ```

- 公开仓库连续 60 天没有提交，GitHub 会自动停用定时任务；工作流在空闲时会每 30 天补一个空提交保活。
