---
name: xhs-note-upload
description: 小红书笔记上传到飞书多维表格发布队列（影刀RPA自动发布）。Use when the user wants to 发布/上传小红书笔记、把笔记写进多维表格/发布队列, 检查或修复表格模板字段（模板体检）, 清理标签选项, 检查待发布队列/空行（队列体检）, 或提到 影刀发布、发布账号、笔记数据表。Handles title ≤20 chars check with rewrite-confirm, tag splitting into separate multi-select options, image order & attachment upload, account validation against 设置表.
---

# 小红书笔记上传（xhs-note-upload）

所有对表格的读写都通过本 skill 的脚本完成，脚本路径：
`%USERPROFILE%\.claude\skills\xhs-note-upload\scripts\xhs_bitable.py`（下称「脚本」）。

每个子命令的 **stdout 是且只是一个 JSON 对象** `{"ok", "action", "data", "errors", "warnings"}`，stderr 是过程日志。只解析 stdout 的 JSON 作为结果；`errors[].message` 已是可直接展示给用户的中文。

## §0 预检（每次会话第一次用本 skill 时执行）

1. 确认 Python 可用：`python --version`（不行再试 `py -3 --version`）。若缺 `requests`，征得用户同意后 `pip install requests`。
2. 运行 `python <脚本> check-config`：
   - `ok:true` → 记住它输出的表格信息，继续。
   - `CONFIG_MULTIPLE` → 把 `data.candidates` 列给用户选，之后所有命令都带 `--config <所选路径>`。
   - `CONFIG_NOT_FOUND` → 按 `data.guidance` 引导：让用户打开影刀文件夹里的 config.json（通常在 `桌面\影刀文件夹\小红书发布\图文发布\`），说出/粘贴 `table_url` 和 `auth_code`（pt- 开头），然后运行
     `python <脚本> save-config --table-url "<URL>" --auth-code "<pt-…>"` 保存，之后自动生效。

## §1 上传笔记（核心流程）

1. **收集信息**。必填：标题、正文、笔记类型（只提供「图文 / 视频」两个选项）、发布账号、图片或视频文件路径（问清图片的先后顺序）。可选：标签、定时发布（yyyy-MM-dd HH:mm）、地点、提及用户、关联商品、关联群聊。
   - 收集发布账号前先运行 `list-accounts`，把合法账号列给用户选，不要让用户凭记忆填。
   - **封面规则（收图时主动讲给用户听，用户对封面有疑问时也用它解释）**：图文笔记的配图顺序由图片**文件名**决定，从小到大排，排最前的那张就是封面。例如同时有 图2.jpg 和 图1.jpg，图1.jpg 会排在前面上传，它就是封面。视频笔记封面固定取视频第一帧，不支持另外设置。所以要问清用户想要的顺序和封面是哪张；如果用户给的顺序和文件名排序不一致，可以帮他整理——脚本会自动把图片复制重命名成 01_/02_/…（按用户给的顺序）再上传，确保封面就是用户想要的那张，重命名方案会在预览里展示。
   - **可选字段只在用户明确提供时才填**：地点、提及用户、关联商品、关联群聊、定时发布——用户没说就留空，不要自己推断代填（比如用户没提地点，就绝不能因为内容涉及某城市而写上地点）。
2. **写 payload**：把收集到的内容写成 UTF-8 JSON 文件放到会话 scratchpad 目录（键名用中文：标题/正文/标签/笔记类型/发布账号/文件/定时发布/地点/提及用户/关联商品/关联群聊）。**严禁把中文内容直接放在命令行参数里传**。标签可以整串给（脚本会自动按 #、空格、逗号、顿号拆成一个个独立选项）。
3. **校验**：`python <脚本> validate --payload <文件>`。
   - `TITLE_TOO_LONG` → 给用户 1–3 个 ≤20 字符的改写建议（保留原标题的钩子和关键词），让用户选一个或自己改，**绝不静默截断**；更新 payload 后重新 validate。
   - 其它错误（账号不存在、文件找不到、标签疑似正文等）→ 把 message 转述给用户，对话解决后重新 validate。
4. **预览确认**：validate 通过后，向用户展示摘要——标题（含字数）、笔记类型、发布账号、最终标签列表（标出哪些是新建选项）、图片顺序（标出封面；如有 `FILES_RENAMED` 警告，说明会按用户给的顺序重命名为 01_/02_/… 上传）、定时发布时间。**用户明确确认后**才继续。
5. **上传**：`python <脚本> upload --payload <文件>`。成功后向用户报告 record_id 和回读快照。
   - 若 `data.attachment_fallback` 为 true：记录已创建但没有附件。必须明确告诉用户——打开表格，按标题找到该行，**只**把图片/视频拖进「封面及配图」单元格（别的都不要碰），并且要**在影刀 RPA 下次运行之前完成**，否则该行是残缺行会让 RPA 报错。

## §2 模板体检/修复

1. `python <脚本> schema-check` → 展示报告：正常字段、疑似被改名（renamed_guess）、缺失（missing）、类型被改（type_mismatch，API 修不了，需要手动在表格里改回）。
2. 用户确认要修哪些后，把修复计划写成 JSON 文件（scratchpad）：`{"rename": [{"field_id": "fldXXX", "to": "标题"}], "create": ["地点"]}`，运行 `schema-check --fix <文件>`。
3. 「比特浏览器窗口ID」是公式引用字段，API 无法重建；如果它丢了，按 `references/table-schema.md` 的步骤指导用户手动重建。

## §3 标签选项清理

1. `python <脚本> clean-tags` → 向用户展示：脏选项数量和示例（garbage_preview）、多少条记录需要改写（remap，会把脏选项拆成规范标签）、多少个选项直接删（delete_unreferenced）、被丢弃的正文片段。
2. **用户确认后**，把输出里的 `data.plan` 原样存成 JSON 文件，运行 `clean-tags --apply <文件>`，然后报告前后选项数。

## §4 待发布队列体检

1. `python <脚本> queue-check` → 三组结果：`blank`（空行，删除候选）、`incomplete`（残缺行，附各自问题清单）、`complete`。判定口径与 RPA 拉取条件一致：发布任务提交时间为空 且 已发布未勾选。
2. 残缺行：和用户逐条把缺的信息补齐，写成 patch JSON（只含要改的键；改图片用「文件」键），运行 `update-record --record-id <id> --payload <文件>`。
3. 空行：先把「标题 + record_id」清单展示给用户，**用户明确确认后**才 `delete-records --ids id1,id2`。

## 硬规则

- 永远不写这三个字段：**已发布、发布任务提交时间、比特浏览器窗口ID**（RPA/公式专属；脚本层也会拦截）。
- 一切修改动作（上传、修复、清理、删除）都必须"先报告 → 用户确认 → 再执行"。
- 标签规则：一个标签一个选项，带 # 不带 # 都可以；脚本会自动与已有选项去重归一。
- 不要让用户手动去表格里改任何东西（附件 fallback 是唯一例外）。
