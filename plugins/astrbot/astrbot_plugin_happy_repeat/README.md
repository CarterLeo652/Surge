# 欢乐复读（AstrBot 插件）

多位群成员连续发送相同消息时，机器人参与复读。

## 安装

将本目录 `astrbot_plugin_happy_repeat` 放入 AstrBot 的 `data/plugins/`，然后重载插件或重启 AstrBot。也可以将本目录单独压缩为 ZIP，在 AstrBot 管理面板「插件」→「安装插件」中上传。只支持 AstrBot 内置 Telegram 适配器。已安装旧名称插件时，先移除旧插件目录，避免同时运行两个实例；群白名单等配置需要在新插件中重新设置。

要让机器人收到普通群消息，需按 [AstrBot Telegram 接入说明](https://docs.astrbot.app/platform/telegram.html) 在 BotFather 使用 `/setprivacy` → `Disable`，并将机器人加入目标群。

在仓库根目录运行 `python3 -m unittest discover -s plugins/astrbot/astrbot_plugin_happy_repeat/tests` 可执行本地逻辑测试。

## 行为

- 默认同一群（Telegram 话题单独计数）中，两位不同的成员在 120 秒内连续发送相同消息后，机器人复读最新一条一次。
- 转发来源不参与相等判定：普通消息与转发消息的正文相同，也算连续复读。
- 支持文本、图片、贴纸、动画、视频、语音、音频和文件；转发消息优先按 Telegram 转发方式发送，其他消息用 Telegram 拷贝方式发送，以保留媒体和文本实体。
- 相同内容持续刷屏只复读一次；不同消息会开始新一轮；命令和其他机器人消息会打断计数。
- 普通群中，Telegram 消息 ID 出现间隔也会打断计数，以免图片等消息比后续文字更晚进入插件时误判为连续复读。话题群不使用此检查，因为不同话题共用群消息 ID。
- 默认不复读带链接的消息。可以在 AstrBot 插件配置里关闭 `block_links`。可设置群白名单、黑名单、参与人数和时间窗。
- 安装后默认对所有 Telegram 群生效。如果只想在指定群使用，请先填写 `enabled_groups`。
- 默认关闭 `[欢乐复读诊断]` 日志。排障时可打开 `debug_logging`，查看群、消息 ID、发送者和判定原因。已安装旧版的配置可能保留原值，需要在插件设置中手动关闭一次。

## 使用说明

Telegram 限制复制或转发的消息若发送失败，插件会记录日志并等待下一条相同消息再尝试。群聊状态只在内存中保存，重载或重启后重新计数。
