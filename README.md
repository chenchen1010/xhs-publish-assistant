# xhs-note-upload — 小红书笔记上传 Skill（Claude Code）

把下面这段话直接复制给 Claude Code，它会替你完成安装和验证：

> 请安装并使用 https://github.com/chenchen1010/xhs-note-upload 这个仓库的 Skill（小红书笔记上传到飞书多维表格发布队列）。只处理我本人电脑上的文件和我自己的飞书表格授权码。请先完成平台识别、Python 和 requests 依赖检查，把 Skill 装到我的 Claude Code skills 目录，然后运行 check-config 验证能连上我的表格；找不到配置就引导我提供表格链接和授权码。验证成功后再问我要发布什么笔记。过程中不要上传或打印我的授权码。

## 它解决什么问题

如果你在用「小红书多维表格发布应用」（飞书多维表格当发布队列 + 影刀 RPA 自动发布），手动往表格里填笔记很容易出错：

- 标题超过 20 个字符，发布失败
- 一堆标签挤进一个多选选项，页面上看不出来，发出去就是错的
- 不小心改了字段名，RPA 直接找不到数据
- 留下空行、半成品行——RPA 的拉取条件是「发布任务提交时间为空且未发布」，这些行会被拉走并报错

装上这个 Skill 后，你不再碰表格：跟 Claude 对话，它替你校验（标题长度、标签拆分、账号是否存在、图片顺序），预览给你确认，然后写进表格。另外附带三个维护功能：模板体检/修复、标签脏选项清理、待发布队列体检。

## 前置条件

- 你已经在用这套发布应用，电脑上有它的 `config.json`（内含表格链接 `table_url` 和 `pt-` 开头的授权码 `auth_code`，通常在 `桌面/影刀文件夹/小红书发布/图文发布/` 里）
- Claude Code（命令行版或桌面版）
- Python 3.7+，以及 `requests` 库（`pip install requests`）

## 安装

**Windows**（命令行执行）：

```
git clone https://github.com/chenchen1010/xhs-note-upload.git "%USERPROFILE%\.claude\skills\xhs-note-upload"
pip install requests
```

**macOS**：

```
git clone https://github.com/chenchen1010/xhs-note-upload.git ~/.claude/skills/xhs-note-upload
pip3 install requests
```

不想用 git 的话：仓库页面 Code → Download ZIP，解压后把 `xhs-note-upload` 文件夹放进上面路径的 `skills` 目录即可。

## 验证（三层，逐层确认）

1. **安装成功**：运行
   `python "%USERPROFILE%\.claude\skills\xhs-note-upload\scripts\xhs_bitable.py" check-config`
   （macOS 用 `python3 ~/.claude/skills/...`）。只要它输出一段 JSON——哪怕是 `CONFIG_NOT_FOUND`——脚本和依赖就已就绪。
2. **配置成功**：上一步返回 `"ok": true`，且 `note_table_ok`、`settings_table_ok` 都为 true，说明已连上你的表格。找不到配置时，把 config.json 里的表格链接和授权码告诉 Claude，它会用 save-config 帮你保存，以后自动生效。
3. **上传可用**：对 Claude 说「帮我传一篇测试笔记，传完删掉」。它会走完整链路——校验 → 预览确认 → 写入表格 → 回读核对 → 删除测试记录。这一步通过，才算真正可用。

## 日常怎么用

| 你想做什么 | 对 Claude 说 |
| --- | --- |
| 发一篇笔记 | 帮我发一篇小红书笔记 |
| 检查队列里的空行/残缺行 | 检查一下待发布队列 |
| 清理标签里的脏选项 | 清理一下标签选项 |
| 检查字段是否被改坏 | 检查一下表格模板 |

它会遵守的规则：

- 标题超 20 字符：给你改写建议，你确认了才上传，绝不静默截断
- 标签：一个标签一个选项，带不带 # 都行，自动和已有选项去重
- 封面：图文笔记按图片**文件名**从小到大排序，排最前的是封面；顺序不对它会帮你重命名后再传；视频笔记封面固定取第一帧
- 发布账号：只能从你「设置」表里已有的账号里选
- 永远不碰这三个字段：已发布、发布任务提交时间、比特浏览器窗口ID
- 一切写入、修复、删除动作都先报告、经你确认才执行

## 边界与已知限制

- 笔记类型目前仅支持图文和视频（RPA 限制）
- 单个文件最大 20MB；超出时记录会先建好，需要你手动把文件拖进该行的「封面及配图」单元格
- 定时发布用的是电脑系统定时，不是小红书官方定时，届时电脑和影刀需在运行状态
- 授权码只保存在你本机（影刀的 config.json 或本 Skill 目录下的 config.local.json，后者已被 .gitignore 排除）

## 仓库内文件

- `SKILL.md` — Claude 执行的工作流（Skill 本体）
- `scripts/xhs_bitable.py` — 后端脚本，全部表格读写都经它，唯一依赖 requests
- `references/table-schema.md` — 发布模板的字段基准（含「比特浏览器窗口ID」公式字段的手动重建步骤）
- `README-客户安装指南.md` — 线下分发给客户的简化版说明

## 问题反馈

用下来有问题或想要的功能，欢迎在 [Issues](https://github.com/chenchen1010/xhs-note-upload/issues) 里提~
