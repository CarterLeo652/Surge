# AstrBot VPS 网络工具

移植 TeleBox 的 IP、BGP、WHOIS、dig 四项查询功能，注册四个 Telegram 菜单顶层命令。查询本身不需要 API Key。

插件列表图标使用根目录的 `logo.png`。

| 命令 | 功能 |
| --- | --- |
| `/vps_ip 1.1.1.1` | 查询 IP 或域名的位置、ISP、组织、AS、时区以及可用的代理／机房标记；可回复消息提取目标 |
| `/vps_bgp 1.1.1.1` | 从 bgp.tools 获取 BGP 路由 SVG，尝试 /24、/23 前缀并转 PNG；在 Telegram 中以一张带说明的图片发送；可回复含 IP 的消息 |
| `/vps_bgp dns 1.1.1.1` | 查询 bgp.tools 前缀页中的 DNS 记录；也可回复消息 |
| `/vps_whois example.com` | 通过 namebeta 查询原始 WHOIS 和注册摘要；支持 URL 或回复消息 |
| `/vps_whois batch a.com b.com` | 批量查询，最多 10 个域名 |
| `/vps_whois history` | 查看最近 20 条查询历史，最多保存 100 条 |
| `/vps_whois clear` | 清除历史及缓存 |
| `/vps_dig example.com MX` | 调用系统 `dig` 查询 DNS；默认简短输出，前 3 条结果中的 IPv4 会附归属地 |
| `/vps_dig example.com A +noall +answer` | 查看详细 DNS 应答 |
| `/vps_dig example.com @1.1.1.1` | 指定 DNS 服务器 |
| `/vps_dig -x 1.1.1.1` | 反向 DNS 查询 |

IP、BGP、WHOIS 在缺少参数时显示原版对应的帮助或错误；dig 返回“未提供任何参数”。WHOIS 查询结果缓存 24 小时。dig 与原版一样按参数数组调用系统命令，无选项时自动加 `+short`，只拒绝 Shell 特殊字符和路径穿越。dig 的 IPv4 归属地优先查询 ip.sb，请求失败时回退到 ipinfo.io。

Telegram 的 AstrBot 适配器会分别发送图片消息段和文字消息段。BGP 图片因此以一条带说明的照片消息发出；根据 TeleBox 截图，将 bgp.tools 的竖长路由图放在浅色方形画布中。IP、WHOIS、dig 和 BGP DNS 的结果使用与原版相同的 Telegram HTML 模板；WHOIS 原始数据与 BGP DNS 列表可折叠。WHOIS 批量查询会在同一条状态消息中更新进度。其他适配器仍使用普通消息链。

Telegram 气泡的颜色、左右位置和头像由发送账号及客户端决定：TeleBox 以用户账号发送，AstrBot Telegram 适配器以机器人发送，这部分无法由插件改变。

从 v0.3.1 起，Telegram 结果直接发出后会结束该命令事件，避免 AstrBot 再尝试发送仅含零宽字符的空回复。

## 插件设置

在 AstrBot 插件管理中打开本插件的配置，可设置：

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| 自动删除插件回复 | 关闭 | 开启后自动删除本插件在 Telegram 发出的文字、BGP 图片及错误提示；不删除用户命令 |
| 自动删除时间（秒） | 300 | 从最终结果发出或进度消息更新为最终结果时开始计时；有效范围 1–86400 秒 |

自动删除只适用于 Telegram 适配器。删除任务保存在运行中的 AstrBot 进程内；若进程在到期前重启，尚未执行的删除任务不会继续。Telegram 对已发送消息的删除有时间限制，因此插件将配置时间限制在一天内。

## 安装

在 AstrBot WebUI 选择“插件 → 安装插件 → 从文件安装”，上传本插件 ZIP；或把目录复制到 `data/plugins/astrbot_plugin_vps_tools/` 后重载。Telegram 适配器默认会同步四个顶层命令到机器人菜单，前提是 `telegram_command_register` 未关闭。

## 运行依赖

- Python 依赖 `CairoSVG` 会由插件的 `requirements.txt` 安装。生成 BGP PNG 还需要系统的 Cairo 共享库；缺少时请在 AstrBot 所在机器或容器安装 `libcairo2`（Debian/Ubuntu）或对应发行版的 Cairo 包。
- `/vps_dig` 需要系统安装 `dig`：Debian/Ubuntu 安装 `dnsutils`，RHEL/Fedora 安装 `bind-utils`。如果 AstrBot 在容器内运行，需要安装到容器内。
- BGP 图片和 DNS 记录依赖 bgp.tools 的页面与 SVG 地址；若上游页面改版，插件会返回查询失败或无结果。
- IP 与原版一样使用 ip-api.com 免费接口（HTTP），域名直接交给该接口解析。地理位置和代理／机房标记以服务返回的数据为准。
- WHOIS 与原版一样使用 namebeta SSE；该服务不可用时返回错误，不切换到其他数据源。

本插件仅提供查询功能，不修改 VPS 配置。WHOIS 历史与缓存保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_vps_tools/` 下。

功能依据 [TeleBox-Plugins](https://github.com/TeleBoxOrg/TeleBox-Plugins) 中的 `ip`、`bgp`、`whois`、`dig` 插件重新实现，接口与消息发送方式适配 AstrBot。
