# Telegram 机器人消息过滤

本插件只在 AstrBot 的 Telegram 适配器上工作。收到消息时，它读取 Telegram 原始消息的发送者 `from_user.is_bot`：值为 `True` 时停止事件传播，不触发后续插件处理或 AI 回复；真人发送的消息保持原样。

另外，当 AstrBot 准备发送的 Telegram 回复只含引用/@ 标头和不可见字符 `U+200C` 时，插件会跳过这条 Telegram 本来就会拒绝的回复。这是为避免 `Text must be non-empty` 日志错误；有可见文字、图片或其他内容的回复不受影响。

## 安装

将整个 `astrbot_plugin_ignore_tg_bots` 目录放入 AstrBot 的 `data/plugins/`，然后重载插件或重启 AstrBot。也可以先将本目录连同顶层目录名压缩为 ZIP，在网页后台「插件」→「安装插件」→「从文件安装」中上传。安装后确认插件显示为已启用。

不需要修改 BotFather 设置，也不需要额外的 Python 依赖。

## 验证

在 Telegram 群中让另一个 Bot 发一条消息：AstrBot 应不回复。再让真人发一条平时会触发 AstrBot 回复的消息：应仍能正常回复。插件只依据实际发送者判断；真人转发或引用 Bot 消息仍按真人消息处理。

如果之前会产生 `Reply + Plain('\u200c')` 的无效回复，再次触发时应不再出现 `Text must be non-empty`。插件不会阻止真人发送给其他 Bot 的斜杠命令被 AstrBot 接收；如需忽略那类命令，需要另设规则。
