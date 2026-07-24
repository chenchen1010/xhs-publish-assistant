# -*- coding: utf-8 -*-
"""xhs_bitable.py — 小红书笔记发布 skill 后端脚本

通过飞书多维表格 API（Personal Base Token）完成：
  校验并上传笔记到「笔记数据」表、模板体检/修复、标签选项清理、待发布队列体检。

I/O 契约：每个子命令 stdout 只输出一个 JSON 对象
  {"ok": bool, "action": str, "data": {...}, "errors": [...], "warnings": [...]}
日志走 stderr。唯一第三方依赖：requests。
"""
import argparse
import base64
import calendar
import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def emit(action, ok, data=None, errors=None, warnings=None):
    print(json.dumps({
        "ok": bool(ok),
        "action": action,
        "data": data or {},
        "errors": errors or [],
        "warnings": warnings or [],
    }, ensure_ascii=False, default=str))
    sys.exit(0 if ok else 1)


try:
    import requests
except ImportError:
    emit("init", False, errors=[{"code": "MISSING_DEPENDENCY",
                                 "message": "缺少 requests 库，请先运行: pip install requests"}])

SKILL_DIR = Path(__file__).resolve().parent.parent
BASE_HOST = "https://base-api.feishu.cn"
NOTE_TABLE_NAME = "笔记数据"
SETTINGS_TABLE_NAME = "设置"

FORBIDDEN_FIELDS = {"已发布", "发布任务提交时间", "比特浏览器窗口ID"}
USER_FIELDS = {"标题", "正文", "标签", "笔记类型", "发布账号", "文件", "定时发布",
               "地点", "提及用户", "关联商品", "关联群聊", "所属平台"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov"}
MAX_FILE_BYTES = 20 * 1024 * 1024
TITLE_MAX = 20
BODY_MAX = 1000

WINDOW_ID_KEYS = ["比特浏览器窗口ID", "窗口ID", "窗口 ID"]

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# 「笔记数据」表模板基准（与线上模板一致；type: 1=文本 3=单选 4=多选 5=日期 7=复选 17=附件 19=公式引用）
CANONICAL_NOTE_FIELDS = [
    {"field_name": "标题", "type": 1},
    {"field_name": "正文", "type": 1},
    {"field_name": "标签", "type": 4, "create_property": {"options": []}},
    {"field_name": "封面及配图", "type": 17},
    {"field_name": "定时发布", "type": 5,
     "create_property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}},
    {"field_name": "地点", "type": 1},
    {"field_name": "提及用户", "type": 4, "create_property": {"options": []}},
    {"field_name": "笔记类型", "type": 3,
     "create_property": {"options": [{"name": "图文"}, {"name": "视频"},
                                     {"name": "长文"}, {"name": "文字配图"}]}},
    {"field_name": "发布账号", "type": 4, "create_property": {"options": []}},
    {"field_name": "比特浏览器窗口ID", "type": 19, "no_create": True},
    {"field_name": "已发布", "type": 7},
    {"field_name": "发布任务提交时间", "type": 1},
    {"field_name": "所属平台", "type": 3,
     "create_property": {"options": [{"name": "小红书"}, {"name": "抖音"}, {"name": "公众号"}]}},
    {"field_name": "关联商品", "type": 1},
    {"field_name": "关联群聊", "type": 1},
]
CANONICAL_SETTINGS_FIELDS = [
    {"field_name": "发布账号", "type": 1},
    {"field_name": "比特浏览器窗口ID", "type": 1},
    {"field_name": "是否打开每日数据监控", "type": 7},
]


def log(msg):
    print(msg, file=sys.stderr)


def err(code, message, **extra):
    d = {"code": code, "message": message}
    d.update(extra)
    return d


# ---------------------------------------------------------------- config 探测

def _read_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    url = str(cfg.get("table_url", ""))
    code = str(cfg.get("auth_code", ""))
    if "/base/" in url and code.startswith("pt-"):
        return {"table_url": url, "auth_code": code}
    return None


def _walk_configs(root, max_depth):
    root = Path(root)
    if not root.exists():
        return
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            low = fn.lower()
            if low.startswith("config") and low.endswith(".json"):
                yield Path(dirpath) / fn


def discover_config(explicit=None):
    """返回 (path, cfg, candidates)。多候选时 cfg=None、candidates 非空。"""
    home = Path(os.path.expanduser("~"))
    tried = []

    for source, p in [
        ("--config 参数", explicit),
        ("环境变量 XHS_SKILL_CONFIG", os.environ.get("XHS_SKILL_CONFIG")),
        ("skill 本地配置", SKILL_DIR / "config.local.json"),
        ("桌面标准路径", home / "Desktop" / "影刀文件夹" / "小红书发布" / "图文发布" / "config.json"),
    ]:
        if not p:
            continue
        p = Path(p)
        tried.append(str(p))
        cfg = _read_config(p)
        if cfg:
            return str(p), cfg, []
        if source == "--config 参数":
            return None, None, []  # 显式指定但无效，直接失败

    # 限深搜索
    seen, candidates = set(), []
    for root, depth in [(home / "Desktop" / "影刀文件夹", 4), (home / "Desktop", 4)]:
        for p in _walk_configs(root, depth):
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            if _read_config(p):
                candidates.append(rp)
    # 优先名字就叫 config.json 的
    exact = [c for c in candidates if os.path.basename(c).lower() == "config.json"]
    pool = exact if exact else candidates
    if len(pool) == 1:
        return pool[0], _read_config(pool[0]), []
    if len(pool) > 1:
        return None, None, pool
    return None, None, []


CONFIG_GUIDANCE = (
    "没有找到 config.json。请引导用户提供两样东西："
    "1) 多维表格链接 table_url（形如 https://xxx.feishu.cn/base/xxxx?table=tblxxxx）；"
    "2) 授权码 auth_code（pt- 开头，在影刀发布应用的 config.json 里，"
    "通常位于 桌面\\影刀文件夹\\小红书发布\\图文发布\\config.json）。"
    "拿到后运行 save-config --table-url <URL> --auth-code <pt-…> 保存，之后自动生效。"
)


# ---------------------------------------------------------------- API 客户端

class BitableError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"code={code}: {msg}")
        self.code = code
        self.msg = msg


class BitableClient:
    OK_CODES = {0, 1254606}
    RETRY_CODES = {1254290, 1254291}

    def __init__(self, table_url, auth_code):
        m = re.search(r"/base/([A-Za-z0-9]+)", table_url)
        if not m:
            raise BitableError(-1, "table_url 里没有 /base/xxx 形式的 app_token")
        self.app_token = m.group(1)
        m2 = re.search(r"[?&]table=([A-Za-z0-9]+)", table_url)
        self.url_table_id = m2.group(1) if m2 else None
        self.base_url = "%s/open-apis/bitable/v1/apps/%s" % (BASE_HOST, self.app_token)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": "Bearer " + auth_code})

    def request(self, method, url, json_body=None, params=None, data=None,
                files=None, timeout=60):
        last = None
        for attempt in range(5):
            try:
                resp = self.session.request(method, url, json=json_body, params=params,
                                            data=data, files=files, timeout=timeout)
                body = resp.json()
            except Exception as e:
                last = e
                time.sleep(1 + attempt)
                continue
            code = body.get("code", -1)
            if code in self.OK_CODES:
                return body.get("data") or {}
            if code in self.RETRY_CODES:
                last = BitableError(code, body.get("msg", ""))
                time.sleep(1.5 * (attempt + 1))
                continue
            raise BitableError(code, body.get("msg", ""))
        if isinstance(last, BitableError):
            raise last
        raise BitableError(-1, "网络请求失败: %s" % last)

    def _paginate(self, url, key, page_size):
        items, token = [], None
        while True:
            params = {"page_size": page_size}
            if token:
                params["page_token"] = token
            data = self.request("GET", url, params=params)
            items.extend(data.get(key) or data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = data.get("page_token")
            if not token:
                return items

    def tables(self):
        return self._paginate(self.base_url + "/tables", "items", 100)

    def table_id_by_name(self, name, tables=None):
        for t in (tables if tables is not None else self.tables()):
            if t.get("name") == name:
                return t.get("table_id")
        return None

    def fields(self, table_id):
        return self._paginate("%s/tables/%s/fields" % (self.base_url, table_id), "items", 100)

    def records(self, table_id):
        return self._paginate("%s/tables/%s/records" % (self.base_url, table_id), "items", 500)

    def get_record(self, table_id, record_id):
        try:
            data = self.request("GET", "%s/tables/%s/records/%s"
                                % (self.base_url, table_id, record_id))
            rec = data.get("record")
            if rec:
                return rec
        except BitableError:
            pass
        for r in self.records(table_id):
            if r.get("record_id") == record_id:
                return r
        raise BitableError(-1, "找不到记录 %s" % record_id)

    def batch_create(self, table_id, records):
        out = []
        for i in range(0, len(records), 500):
            data = self.request("POST", "%s/tables/%s/records/batch_create"
                                % (self.base_url, table_id),
                                json_body={"records": records[i:i + 500]})
            out.extend(data.get("records") or [])
        return out

    def batch_delete(self, table_id, record_ids):
        for i in range(0, len(record_ids), 500):
            self.request("POST", "%s/tables/%s/records/batch_delete"
                         % (self.base_url, table_id),
                         json_body={"records": record_ids[i:i + 500]})

    def update_record(self, table_id, record_id, fields):
        return self.request("PUT", "%s/tables/%s/records/%s"
                            % (self.base_url, table_id, record_id),
                            json_body={"fields": fields})

    def update_field(self, table_id, field_id, body):
        return self.request("PUT", "%s/tables/%s/fields/%s"
                            % (self.base_url, table_id, field_id), json_body=body)

    def create_field(self, table_id, body):
        return self.request("POST", "%s/tables/%s/fields"
                            % (self.base_url, table_id), json_body=body)

    def upload_media(self, path, upload_name, parent_type):
        size = os.path.getsize(path)
        url = BASE_HOST + "/open-apis/drive/v1/medias/upload_all"
        with open(path, "rb") as f:
            resp = self.session.post(url, data={
                "file_name": upload_name,
                "parent_type": parent_type,
                "parent_node": self.app_token,
                "size": str(size),
                "extra": json.dumps({"drive_route_token": self.app_token}),
            }, files={"file": (upload_name, f)}, timeout=300)
        body = resp.json()
        if body.get("code") != 0:
            raise BitableError(body.get("code"), body.get("msg", ""))
        return body["data"]["file_token"]


# ---------------------------------------------------------------- 单元格取值

def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        if "text" in v:
            return str(v["text"])
        if "value" in v:
            return cell_text(v["value"])
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "".join(cell_text(x) for x in v)
    return str(v)


def cell_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def is_pending(fields):
    submitted = cell_text(fields.get("发布任务提交时间")).strip()
    return (not submitted) and (fields.get("已发布") is not True)


def ms_to_str(ms):
    if not ms:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000 + 8 * 3600))
    except Exception:
        return str(ms)


# ---------------------------------------------------------------- 标签处理

TAG_SPLIT_RE = re.compile(r"[#\s,，、;；]+")
PARAGRAPH_CHAR_RE = re.compile(r"[。！？!?…：:\n\r]")


def split_tags(raw):
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        if item is None:
            continue
        for tok in TAG_SPLIT_RE.split(str(item)):
            tok = tok.strip()
            if tok and tok not in out:
                out.append(tok)
    return out


def tag_is_paragraph(tok):
    return len(tok) > 30 or bool(PARAGRAPH_CHAR_RE.search(tok))


def tag_key(name):
    return str(name).lstrip("#").strip().lower()


def normalize_tags(tokens, existing_names):
    mapping = {}
    for name in existing_names:
        k = tag_key(name)
        if k and k not in mapping:
            mapping[k] = name
    result, seen = [], set()
    for tok in tokens:
        k = tag_key(tok)
        if not k or k in seen:
            continue
        seen.add(k)
        if k in mapping:
            result.append({"name": mapping[k], "new": False})
        else:
            result.append({"name": "#" + str(tok).lstrip("#").strip(), "new": True})
    return result


def option_is_garbage(name):
    s = str(name)
    if s.count("#") >= 2 or len(s) > 30 or PARAGRAPH_CHAR_RE.search(s):
        return True
    return len(split_tags(s)) >= 2


# ---------------------------------------------------------------- 时间解析

def parse_schedule(s):
    s = str(s).strip().replace("/", "-").replace("T", " ")
    s = re.sub(r"\s+", " ", s)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if not m:
        return None
    try:
        dt = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    except ValueError:
        return None
    return int((calendar.timegm(dt.timetuple()) - 8 * 3600) * 1000)


# ---------------------------------------------------------------- 上下文

class Context:
    def __init__(self, args, need_settings=True):
        path, cfg, candidates = discover_config(getattr(args, "config", None))
        if candidates:
            emit(args.action, False, data={"candidates": candidates},
                 errors=[err("CONFIG_MULTIPLE",
                             "找到多个可用的 config，请让用户选择其一后用 --config 指定")])
        if not cfg:
            emit(args.action, False, data={"guidance": CONFIG_GUIDANCE},
                 errors=[err("CONFIG_NOT_FOUND", "未找到有效的 config.json")])
        self.config_path = path
        self.client = BitableClient(cfg["table_url"], cfg["auth_code"])
        self.tables = self.client.tables()
        table_ids = {t.get("table_id") for t in self.tables}
        # 笔记表：优先 URL 里的 table 参数，其次按表名找
        self.note_table_id = None
        if self.client.url_table_id in table_ids:
            self.note_table_id = self.client.url_table_id
        if not self.note_table_id:
            self.note_table_id = self.client.table_id_by_name(NOTE_TABLE_NAME, self.tables)
        if not self.note_table_id:
            emit(args.action, False, errors=[err(
                "NOTE_TABLE_MISSING",
                "找不到笔记表：URL 里的 table 参数无效，表格里也没有「%s」表" % NOTE_TABLE_NAME)])
        self.settings_table_id = self.client.table_id_by_name(SETTINGS_TABLE_NAME, self.tables)
        if need_settings and not self.settings_table_id:
            emit(args.action, False, errors=[err(
                "SETTINGS_TABLE_MISSING",
                "表格里没有「设置」表，无法校验发布账号；请先在多维表格中恢复「设置」表")])

    def settings_accounts(self):
        """返回 {发布账号: 窗口ID字符串}"""
        result = {}
        if not self.settings_table_id:
            return result
        for r in self.client.records(self.settings_table_id):
            f = r.get("fields") or {}
            name = cell_text(f.get("发布账号")).strip()
            if not name:
                continue
            wid = ""
            for k in WINDOW_ID_KEYS:
                wid = cell_text(f.get(k)).strip()
                if wid:
                    break
            result[name] = wid
        return result

    def tag_field(self):
        for f in self.client.fields(self.note_table_id):
            if f.get("field_name") == "标签":
                return f
        return None


# ---------------------------------------------------------------- 校验

def load_payload(path):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return payload


def as_str_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    return [str(x).strip() for x in v if str(x).strip()]


def validate_payload(ctx, payload, existing_attachments=0, exclude_record_id=None,
                     partial_keys=None):
    """校验 payload；partial_keys 非 None 时表示补全模式（只有这些键是新提供的）。
    返回 (normalized, errors, warnings)"""
    errors, warnings = [], []

    for k in payload:
        if k in FORBIDDEN_FIELDS:
            errors.append(err("FORBIDDEN_FIELD",
                              "字段「%s」由 RPA/公式管理，禁止写入" % k, field=k))
        elif k not in USER_FIELDS:
            warnings.append(err("UNKNOWN_KEY",
                                "payload 里的「%s」不是模板字段，将被忽略" % k, field=k))

    live_fields = ctx.client.fields(ctx.note_table_id)
    live_names = {f.get("field_name") for f in live_fields}
    need = ["标题", "正文", "标签", "封面及配图", "笔记类型", "发布账号"]
    missing_live = [n for n in need if n not in live_names]
    if missing_live:
        errors.append(err("TEMPLATE_BROKEN",
                          "表格缺少模板字段：%s。字段可能被改名或删除，请先运行模板体检（schema-check）"
                          % "、".join(missing_live), missing=missing_live))

    # 标题
    title = str(payload.get("标题") or "").strip()
    if not title:
        errors.append(err("TITLE_MISSING", "标题必填"))
    elif len(title) > TITLE_MAX:
        errors.append(err("TITLE_TOO_LONG",
                          "标题共 %d 个字符，超过 %d 上限。请给出更短的标题（不要静默截断）"
                          % (len(title), TITLE_MAX), length=len(title), title=title))

    # 正文
    body = str(payload.get("正文") or "").strip()
    if not body:
        errors.append(err("BODY_MISSING", "正文必填"))
    elif len(body) > BODY_MAX:
        warnings.append(err("BODY_LONG",
                            "正文 %d 字，超过小红书 %d 字上限，发布时可能失败或被截断"
                            % (len(body), BODY_MAX)))

    # 笔记类型
    ntype = str(payload.get("笔记类型") or "").strip()
    if not ntype:
        errors.append(err("TYPE_MISSING", "笔记类型必填（图文 或 视频）"))
    elif ntype not in ("图文", "视频"):
        errors.append(err("TYPE_INVALID",
                          "笔记类型「%s」不支持，影刀 RPA 目前仅支持：图文、视频" % ntype))

    # 文件
    files = as_str_list(payload.get("文件"))
    file_kinds = []
    for p in files:
        if not os.path.isfile(p):
            errors.append(err("FILE_NOT_FOUND", "文件不存在：%s" % p, path=p))
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            kind = "other"
            errors.append(err("FILE_TYPE_INVALID",
                              "不支持的文件类型 %s（图片支持 jpg/jpeg/png/webp，视频支持 mp4/mov）：%s"
                              % (ext, p), path=p))
        if os.path.getsize(p) > MAX_FILE_BYTES:
            errors.append(err("FILE_TOO_LARGE",
                              "文件超过 20MB，API 无法直传，请压缩后重试或上传后手动拖入表格：%s" % p,
                              path=p))
        file_kinds.append(kind)

    files_required = partial_keys is None or "文件" in partial_keys
    if files_required and not files and existing_attachments <= 0:
        errors.append(err("FILES_MISSING", "必须提供至少一个图片/视频文件（封面及配图）"))
    if files:
        if ntype == "视频":
            if len(files) != 1 or file_kinds != ["video"]:
                errors.append(err("VIDEO_FILE_RULE", "视频笔记必须且只能提供 1 个视频文件（mp4/mov）"))
        elif ntype == "图文":
            if "video" in file_kinds:
                errors.append(err("IMAGE_FILE_RULE", "图文笔记只能提供图片文件，不能包含视频"))

    # 图片顺序：RPA 按文件名升序取图，最小为封面
    rename_plan = []
    if ntype == "图文" and len(files) >= 2:
        basenames = [os.path.basename(p) for p in files]
        if basenames != sorted(basenames) or len(set(basenames)) != len(basenames):
            rename_plan = [{"from": p, "upload_name": "%02d_%s" % (i + 1, os.path.basename(p))}
                           for i, p in enumerate(files)]
            warnings.append(err("FILES_RENAMED",
                                "图片将按你给的顺序重命名为 01_/02_/… 后上传"
                                "（RPA 按文件名升序取图，第一张为封面）",
                                rename_plan=rename_plan))

    # 标签
    tokens = split_tags(payload.get("标签"))
    for tok in tokens:
        if tag_is_paragraph(tok):
            errors.append(err("TAG_LOOKS_LIKE_PARAGRAPH",
                              "「%s…」不像一个标签（过长或含句读），请确认是否误把正文当成标签" % tok[:20],
                              token=tok))
    tag_field = None
    for f in live_fields:
        if f.get("field_name") == "标签":
            tag_field = f
            break
    existing_opts = []
    if tag_field and isinstance(tag_field.get("property"), dict):
        existing_opts = [o.get("name", "") for o in tag_field["property"].get("options") or []]
    tags_norm = normalize_tags(tokens, existing_opts)
    if len(tags_norm) > 20:
        warnings.append(err("TOO_MANY_TAGS", "共 %d 个标签，建议不超过 10 个" % len(tags_norm)))

    # 发布账号
    accounts = as_str_list(payload.get("发布账号"))
    acc_map = ctx.settings_accounts()
    if partial_keys is None or "发布账号" in partial_keys or accounts:
        if not accounts:
            errors.append(err("ACCOUNT_MISSING", "发布账号必填",
                              valid_accounts=sorted(acc_map)))
        for a in accounts:
            if a not in acc_map:
                errors.append(err("UNKNOWN_ACCOUNT",
                                  "发布账号「%s」在「设置」表里不存在。可选账号：%s"
                                  % (a, "、".join(sorted(acc_map)) or "（设置表为空）"),
                                  account=a, valid_accounts=sorted(acc_map)))
            elif not acc_map[a]:
                warnings.append(err("ACCOUNT_NO_WINDOW",
                                    "账号「%s」在「设置」表里没有填比特浏览器窗口ID，RPA 将打不开浏览器窗口" % a,
                                    account=a))

    # 定时发布
    sched_raw = str(payload.get("定时发布") or "").strip()
    sched_ms = None
    if sched_raw:
        sched_ms = parse_schedule(sched_raw)
        if sched_ms is None:
            errors.append(err("SCHEDULE_INVALID",
                              "定时发布时间「%s」无法解析，请用 yyyy-MM-dd HH:mm 格式" % sched_raw))
        elif sched_ms < time.time() * 1000:
            warnings.append(err("SCHEDULE_PAST", "定时发布时间 %s 已经过去" % sched_raw))

    # 所属平台
    platform = str(payload.get("所属平台") or "小红书").strip() or "小红书"

    # 提及用户
    mentions = []
    for m in as_str_list(payload.get("提及用户")):
        for tok in re.split(r"[@\s,，、]+", m):
            tok = tok.strip()
            if tok and ("@" + tok) not in mentions:
                mentions.append("@" + tok)

    # 待发布队列重名
    if title:
        try:
            for r in ctx.client.records(ctx.note_table_id):
                if exclude_record_id and r.get("record_id") == exclude_record_id:
                    continue
                f = r.get("fields") or {}
                if is_pending(f) and cell_text(f.get("标题")).strip() == title:
                    warnings.append(err("DUPLICATE_TITLE",
                                        "待发布队列里已有同标题笔记（record_id=%s），请确认不是重复上传"
                                        % r.get("record_id"), record_id=r.get("record_id")))
                    break
        except BitableError as e:
            warnings.append(err("DUP_CHECK_SKIPPED", "重复标题检查失败已跳过：%s" % e.msg))

    normalized = {
        "标题": title, "标题字数": len(title),
        "正文": body, "正文字数": len(body),
        "笔记类型": ntype,
        "标签": [t["name"] for t in tags_norm],
        "标签明细": tags_norm,
        "发布账号": accounts,
        "文件": files,
        "rename_plan": rename_plan,
        "定时发布": sched_raw, "定时发布_ms": sched_ms,
        "所属平台": platform,
        "提及用户": mentions,
        "地点": str(payload.get("地点") or "").strip(),
        "关联商品": str(payload.get("关联商品") or "").strip(),
        "关联群聊": str(payload.get("关联群聊") or "").strip(),
    }
    return normalized, errors, warnings


def build_record_fields(normalized, attachment_tokens=None):
    fields = {
        "标题": normalized["标题"],
        "正文": normalized["正文"],
        "笔记类型": normalized["笔记类型"],
        "发布账号": normalized["发布账号"],
        "所属平台": normalized["所属平台"],
    }
    if normalized["标签"]:
        fields["标签"] = normalized["标签"]
    if normalized["提及用户"]:
        fields["提及用户"] = normalized["提及用户"]
    for k in ("地点", "关联商品", "关联群聊"):
        if normalized.get(k):
            fields[k] = normalized[k]
    if normalized.get("定时发布_ms"):
        fields["定时发布"] = normalized["定时发布_ms"]
    if attachment_tokens:
        fields["封面及配图"] = [{"file_token": t} for t in attachment_tokens]
    return fields


def record_snapshot(rec):
    f = rec.get("fields") or {}
    return {
        "record_id": rec.get("record_id"),
        "标题": cell_text(f.get("标题")),
        "正文预览": cell_text(f.get("正文"))[:50],
        "标签": cell_list(f.get("标签")),
        "笔记类型": cell_text(f.get("笔记类型")),
        "发布账号": cell_list(f.get("发布账号")),
        "附件数": len(cell_list(f.get("封面及配图"))),
        "附件名": [a.get("name") for a in cell_list(f.get("封面及配图")) if isinstance(a, dict)],
        "定时发布": ms_to_str(f.get("定时发布")),
        "所属平台": cell_text(f.get("所属平台")),
        "已发布": f.get("已发布") is True,
        "发布任务提交时间": cell_text(f.get("发布任务提交时间")),
    }


def upload_attachments(ctx, normalized, warnings):
    """上传附件，返回 (tokens, fallback, fallback_reason)"""
    specs = []
    tmpdir = None
    if normalized["rename_plan"]:
        tmpdir = tempfile.mkdtemp(prefix="xhs_upload_")
        for item in normalized["rename_plan"]:
            dst = os.path.join(tmpdir, item["upload_name"])
            shutil.copyfile(item["from"], dst)
            specs.append((dst, item["upload_name"]))
    else:
        specs = [(p, os.path.basename(p)) for p in normalized["文件"]]

    tokens = []
    try:
        for path, name in specs:
            ext = os.path.splitext(path)[1].lower()
            parent_type = "bitable_image" if ext in IMAGE_EXTS else "bitable_file"
            try:
                tokens.append(ctx.client.upload_media(path, name, parent_type))
                log("已上传 %s" % name)
            except BitableError as e1:
                alt = "bitable_file" if parent_type == "bitable_image" else "bitable_image"
                try:
                    tokens.append(ctx.client.upload_media(path, name, alt))
                    log("已上传 %s（parent_type=%s）" % (name, alt))
                except BitableError as e2:
                    return [], True, "附件上传失败：%s / %s" % (e1.msg, e2.msg)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return tokens, False, None


# ---------------------------------------------------------------- 子命令

def cmd_check_config(args):
    path, cfg, candidates = discover_config(args.config)
    if candidates:
        emit(args.action, False, data={"candidates": candidates},
             errors=[err("CONFIG_MULTIPLE", "找到多个可用 config，请让用户选择后用 --config 指定")])
    if not cfg:
        emit(args.action, False, data={"guidance": CONFIG_GUIDANCE},
             errors=[err("CONFIG_NOT_FOUND", "未找到有效的 config.json")])
    client = BitableClient(cfg["table_url"], cfg["auth_code"])
    tables = client.tables()
    names = [t.get("name") for t in tables]
    table_ids = {t.get("table_id") for t in tables}
    note_ok = (client.url_table_id in table_ids) or (NOTE_TABLE_NAME in names)
    warnings = []
    if SETTINGS_TABLE_NAME not in names:
        warnings.append(err("SETTINGS_TABLE_MISSING", "表格里没有「设置」表，发布账号将无法校验"))
    emit(args.action, True, data={
        "config_path": path,
        "app_token": client.app_token,
        "url_table_id": client.url_table_id,
        "tables": names,
        "note_table_ok": note_ok,
        "settings_table_ok": SETTINGS_TABLE_NAME in names,
    }, warnings=warnings)


def cmd_save_config(args):
    if "/base/" not in args.table_url:
        emit(args.action, False, errors=[err("URL_INVALID",
             "表格链接不对：里面应包含 /base/xxxx（在浏览器打开多维表格后复制地址栏链接）")])
    if not args.auth_code.startswith("pt-"):
        emit(args.action, False, errors=[err("AUTH_INVALID", "授权码应以 pt- 开头")])
    client = BitableClient(args.table_url, args.auth_code)
    try:
        tables = client.tables()
    except BitableError as e:
        emit(args.action, False, errors=[err("PROBE_FAILED",
             "用该链接+授权码访问表格失败（code=%s: %s），请检查授权码是否失效" % (e.code, e.msg))])
    dst = SKILL_DIR / "config.local.json"
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"table_url": args.table_url, "auth_code": args.auth_code},
                  f, ensure_ascii=False, indent=2)
    emit(args.action, True, data={"saved_to": str(dst),
                                  "tables": [t.get("name") for t in tables]})


def cmd_list_accounts(args):
    ctx = Context(args)
    acc = ctx.settings_accounts()
    emit(args.action, True, data={"accounts": [
        {"发布账号": k, "has_window_id": bool(v)} for k, v in acc.items()]})


def cmd_list_tags(args):
    ctx = Context(args, need_settings=False)
    field = ctx.tag_field()
    if not field:
        emit(args.action, False, errors=[err("TAG_FIELD_MISSING",
             "笔记表里没有「标签」字段，请先运行模板体检")])
    opts = (field.get("property") or {}).get("options") or []
    emit(args.action, True, data={"total": len(opts), "options": [
        {"id": o.get("id"), "name": o.get("name"),
         "suspect": option_is_garbage(o.get("name", ""))} for o in opts]})


def cmd_validate(args):
    ctx = Context(args)
    payload = load_payload(args.payload)
    normalized, errors, warnings = validate_payload(ctx, payload)
    emit(args.action, not errors, data={"normalized": normalized}, errors=errors,
         warnings=warnings)


def cmd_upload(args):
    ctx = Context(args)
    payload = load_payload(args.payload)
    normalized, errors, warnings = validate_payload(ctx, payload)
    if errors:
        emit(args.action, False, data={"normalized": normalized},
             errors=errors, warnings=warnings)

    tokens, fallback, fb_reason = upload_attachments(ctx, normalized, warnings)
    fields = build_record_fields(normalized, tokens if not fallback else None)
    created = ctx.client.batch_create(ctx.note_table_id, [{"fields": fields}])
    record_id = (created[0] or {}).get("record_id") if created else None
    if not record_id:
        emit(args.action, False, errors=[err("CREATE_FAILED", "创建记录失败（接口未返回 record_id）")])

    snapshot = record_snapshot(ctx.client.get_record(ctx.note_table_id, record_id))
    if fallback:
        warnings.append(err("ATTACHMENT_FALLBACK",
                            "%s。记录已创建但没有附件，必须让用户手动把图片/视频拖进该行的"
                            "「封面及配图」单元格（按标题「%s」找到该行），并在 RPA 下次运行前完成，"
                            "否则该行是残缺行会导致 RPA 报错" % (fb_reason, normalized["标题"])))
    emit(args.action, True, data={
        "record_id": record_id,
        "attachment_fallback": fallback,
        "uploaded_files": len(tokens),
        "snapshot": snapshot,
    }, warnings=warnings)


def cmd_probe_media(args):
    ctx = Context(args, need_settings=False)
    if args.file:
        path, name = args.file, os.path.basename(args.file)
        cleanup = None
    else:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="xhs_probe_")
        with os.fdopen(fd, "wb") as f:
            f.write(TINY_PNG)
        name, cleanup = "probe.png", path
    results = {}
    try:
        for ptype in ("bitable_image", "bitable_file"):
            try:
                token = ctx.client.upload_media(path, name, ptype)
                results[ptype] = {"ok": True, "file_token": token}
            except BitableError as e:
                results[ptype] = {"ok": False, "code": e.code, "msg": e.msg}
    finally:
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass
    ok = any(r["ok"] for r in results.values())
    emit(args.action, ok, data={"results": results,
                                "conclusion": "附件直传可用" if ok else "附件直传不可用，将走 fallback（手动拖图）"})


def cmd_schema_check(args):
    ctx = Context(args, need_settings=False)
    live = ctx.client.fields(ctx.note_table_id)
    canon_by_name = {c["field_name"]: c for c in CANONICAL_NOTE_FIELDS}
    live_by_name = {}
    for f in live:
        live_by_name.setdefault(f.get("field_name"), f)

    ok_fields, type_mismatch, missing = [], [], []
    for c in CANONICAL_NOTE_FIELDS:
        lf = live_by_name.get(c["field_name"])
        if lf is None:
            missing.append(c["field_name"])
        elif lf.get("type") != c["type"]:
            type_mismatch.append({"field": c["field_name"], "expect_type": c["type"],
                                  "actual_type": lf.get("type"),
                                  "note": "字段类型被改，API 无法自动修复，需要在表格里手动改回"})
        else:
            ok_fields.append(c["field_name"])

    extras = [f for f in live if f.get("field_name") not in canon_by_name]
    claimed = set()
    renamed_guess = []
    for name in missing:
        c = canon_by_name[name]
        cands = [e for e in extras
                 if e.get("type") == c["type"] and e.get("field_id") not in claimed]
        if len(cands) == 1:
            claimed.add(cands[0]["field_id"])
            renamed_guess.append({"from": cands[0].get("field_name"),
                                  "field_id": cands[0].get("field_id"),
                                  "to": name, "confidence": "high"})
        elif len(cands) > 1:
            renamed_guess.append({"to": name, "confidence": "low",
                                  "candidates": [{"from": e.get("field_name"),
                                                  "field_id": e.get("field_id")} for e in cands]})
    still_missing = [m for m in missing
                     if not any(g.get("to") == m and g.get("confidence") == "high"
                                for g in renamed_guess)]
    extra_fields = [e.get("field_name") for e in extras if e.get("field_id") not in claimed]

    # 设置表
    settings_report = {"exists": bool(ctx.settings_table_id)}
    if ctx.settings_table_id:
        sf = {f.get("field_name"): f for f in ctx.client.fields(ctx.settings_table_id)}
        settings_report["missing"] = [c["field_name"] for c in CANONICAL_SETTINGS_FIELDS
                                      if c["field_name"] not in sf]

    report = {"ok_fields": ok_fields, "renamed_guess": renamed_guess,
              "missing": still_missing, "type_mismatch": type_mismatch,
              "extra_fields": extra_fields, "settings_table": settings_report,
              "healthy": not (renamed_guess or still_missing or type_mismatch)
                         and settings_report.get("exists")
                         and not settings_report.get("missing")}

    if not args.fix:
        emit(args.action, True, data=report)

    with open(args.fix, encoding="utf-8") as f:
        plan = json.load(f)
    fixed, failed = [], []
    live_by_id = {f.get("field_id"): f for f in live}
    for r in plan.get("rename") or []:
        lf = live_by_id.get(r["field_id"])
        if not lf:
            failed.append({"action": "rename", "target": r, "reason": "field_id 不存在"})
            continue
        body = {"field_name": r["to"], "type": lf.get("type")}
        if isinstance(lf.get("property"), dict):
            body["property"] = lf["property"]
        try:
            ctx.client.update_field(ctx.note_table_id, r["field_id"], body)
            fixed.append("改名：%s → %s" % (lf.get("field_name"), r["to"]))
        except BitableError as e:
            failed.append({"action": "rename", "target": r, "reason": e.msg})
    for name in plan.get("create") or []:
        c = canon_by_name.get(name)
        if not c:
            failed.append({"action": "create", "target": name, "reason": "不是模板字段"})
            continue
        if c.get("no_create"):
            failed.append({"action": "create", "target": name,
                           "reason": "「比特浏览器窗口ID」是引用「设置」表的公式字段，API 无法创建，"
                                     "请按 references/table-schema.md 手动重建"})
            continue
        body = {"field_name": name, "type": c["type"]}
        if c.get("create_property"):
            body["property"] = c["create_property"]
        try:
            ctx.client.create_field(ctx.note_table_id, body)
            fixed.append("补建：%s" % name)
        except BitableError as e:
            failed.append({"action": "create", "target": name, "reason": e.msg})
    emit(args.action, not failed, data={"fixed": fixed, "failed": failed,
                                        "before": report})


def cmd_queue_check(args):
    ctx = Context(args)
    acc_map = ctx.settings_accounts()
    records = ctx.client.records(ctx.note_table_id)
    blank, incomplete, complete = [], [], []
    for r in records:
        f = r.get("fields") or {}
        if not is_pending(f):
            continue
        title = cell_text(f.get("标题")).strip()
        body = cell_text(f.get("正文")).strip()
        tags = cell_list(f.get("标签"))
        atts = cell_list(f.get("封面及配图"))
        accs = [cell_text(a).strip() for a in cell_list(f.get("发布账号"))]
        accs = [a for a in accs if a]
        ntype = cell_text(f.get("笔记类型")).strip()
        info = {"record_id": r.get("record_id"), "标题": title,
                "定时发布": ms_to_str(f.get("定时发布"))}
        if not (body or tags or atts or accs or ntype):
            info["title_only"] = bool(title)
            blank.append(info)
            continue
        problems = []
        if not title:
            problems.append("缺标题")
        elif len(title) > TITLE_MAX:
            problems.append("标题 %d 字超过 %d 上限" % (len(title), TITLE_MAX))
        if not body:
            problems.append("缺正文")
        if not atts:
            problems.append("缺封面及配图")
        if not accs:
            problems.append("缺发布账号")
        else:
            for a in accs:
                if a not in acc_map:
                    problems.append("发布账号「%s」不在设置表里" % a)
                elif not acc_map[a]:
                    problems.append("账号「%s」在设置表里没有窗口ID" % a)
        if not ntype:
            problems.append("缺笔记类型")
        elif ntype not in ("图文", "视频"):
            problems.append("笔记类型「%s」RPA 不支持（仅图文/视频）" % ntype)
        for t in tags:
            if option_is_garbage(cell_text(t)):
                problems.append("标签「%s…」疑似多个标签合并/正文" % cell_text(t)[:15])
                break
        if problems:
            info["problems"] = problems
            incomplete.append(info)
        else:
            complete.append(info)
    emit(args.action, True, data={
        "pending_total": len(blank) + len(incomplete) + len(complete),
        "blank": blank, "incomplete": incomplete, "complete": complete,
        "note": "待发布判定：发布任务提交时间为空 且 已发布未勾选（与 RPA 拉取条件一致）"})


def cmd_update_record(args):
    ctx = Context(args)
    patch = load_payload(args.payload)
    live = ctx.client.get_record(ctx.note_table_id, args.record_id)
    lf = live.get("fields") or {}
    merged = {
        "标题": patch.get("标题", cell_text(lf.get("标题"))),
        "正文": patch.get("正文", cell_text(lf.get("正文"))),
        "标签": patch.get("标签", [cell_text(t) for t in cell_list(lf.get("标签"))]),
        "笔记类型": patch.get("笔记类型", cell_text(lf.get("笔记类型"))),
        "发布账号": patch.get("发布账号",
                          [cell_text(a) for a in cell_list(lf.get("发布账号"))]),
        "所属平台": patch.get("所属平台", cell_text(lf.get("所属平台")) or "小红书"),
        "地点": patch.get("地点", cell_text(lf.get("地点"))),
        "关联商品": patch.get("关联商品", cell_text(lf.get("关联商品"))),
        "关联群聊": patch.get("关联群聊", cell_text(lf.get("关联群聊"))),
    }
    if "文件" in patch:
        merged["文件"] = patch["文件"]
    if "定时发布" in patch:
        merged["定时发布"] = patch["定时发布"]
    if "提及用户" in patch:
        merged["提及用户"] = patch["提及用户"]

    existing_atts = len(cell_list(lf.get("封面及配图")))
    normalized, errors, warnings = validate_payload(
        ctx, merged, existing_attachments=existing_atts,
        exclude_record_id=args.record_id, partial_keys=set(patch.keys()))
    if errors:
        emit(args.action, False, data={"normalized": normalized},
             errors=errors, warnings=warnings)

    update = {}
    if "标题" in patch:
        update["标题"] = normalized["标题"]
    if "正文" in patch:
        update["正文"] = normalized["正文"]
    if "标签" in patch:
        update["标签"] = normalized["标签"]
    if "笔记类型" in patch:
        update["笔记类型"] = normalized["笔记类型"]
    if "发布账号" in patch:
        update["发布账号"] = normalized["发布账号"]
    if "所属平台" in patch:
        update["所属平台"] = normalized["所属平台"]
    if "提及用户" in patch:
        update["提及用户"] = normalized["提及用户"]
    for k in ("地点", "关联商品", "关联群聊"):
        if k in patch:
            update[k] = normalized[k]
    if "定时发布" in patch:
        update["定时发布"] = normalized["定时发布_ms"]

    fallback, fb_reason = False, None
    if "文件" in patch and normalized["文件"]:
        tokens, fallback, fb_reason = upload_attachments(ctx, normalized, warnings)
        if not fallback:
            update["封面及配图"] = [{"file_token": t} for t in tokens]

    if not update:
        emit(args.action, False, errors=[err("EMPTY_PATCH", "payload 里没有可更新的字段")])
    ctx.client.update_record(ctx.note_table_id, args.record_id, update)
    snapshot = record_snapshot(ctx.client.get_record(ctx.note_table_id, args.record_id))
    if fallback:
        warnings.append(err("ATTACHMENT_FALLBACK",
                            "%s。请让用户手动把文件拖进该行「封面及配图」单元格" % fb_reason))
    emit(args.action, True, data={"record_id": args.record_id,
                                  "updated_fields": sorted(update),
                                  "attachment_fallback": fallback,
                                  "snapshot": snapshot}, warnings=warnings)


def cmd_clean_tags(args):
    ctx = Context(args, need_settings=False)
    field = ctx.tag_field()
    if not field:
        emit(args.action, False, errors=[err("TAG_FIELD_MISSING", "笔记表里没有「标签」字段")])

    if args.apply:
        with open(args.apply, encoding="utf-8") as f:
            plan = json.load(f)
        applied, failed = [], []
        for r in plan.get("remap") or []:
            try:
                ctx.client.update_record(ctx.note_table_id, r["record_id"],
                                         {"标签": r["new_tags"]})
                applied.append(r["record_id"])
                time.sleep(0.15)
            except BitableError as e:
                failed.append({"record_id": r.get("record_id"), "reason": e.msg})
        # 重拉字段后按名删除脏选项
        field = ctx.tag_field()
        opts = (field.get("property") or {}).get("options") or []
        delete_names = set(plan.get("delete_options") or [])
        kept = [{"id": o.get("id"), "name": o.get("name"), "color": o.get("color", 0)}
                for o in opts if o.get("name") not in delete_names]
        removed = len(opts) - len(kept)
        try:
            ctx.client.update_field(ctx.note_table_id, field.get("field_id"),
                                    {"field_name": "标签", "type": 4,
                                     "property": {"options": kept}})
        except BitableError as e:
            emit(args.action, False,
                 data={"remapped": applied, "remap_failed": failed},
                 errors=[err("OPTION_DELETE_FAILED", "删除脏选项失败：%s" % e.msg)])
        after = (ctx.tag_field().get("property") or {}).get("options") or []
        emit(args.action, not failed, data={
            "remapped": applied, "remap_failed": failed,
            "options_before": len(opts), "options_removed": removed,
            "options_after": len(after)})

    # 分析模式
    opts = (field.get("property") or {}).get("options") or []
    all_names = [o.get("name", "") for o in opts]
    garbage = [n for n in all_names if option_is_garbage(n)]
    clean_names = [n for n in all_names if n not in garbage]

    referenced = {}
    for r in ctx.client.records(ctx.note_table_id):
        f = r.get("fields") or {}
        tags = [cell_text(t) for t in cell_list(f.get("标签"))]
        bad = [t for t in tags if t in garbage]
        if bad:
            referenced[r.get("record_id")] = {"tags": tags, "bad": bad,
                                              "标题": cell_text(f.get("标题"))}

    remap, dropped_fragments = [], []
    for rid, info in referenced.items():
        keep = [t for t in info["tags"] if t not in garbage]
        tokens = []
        for b in info["bad"]:
            for tok in split_tags(b):
                if tag_is_paragraph(tok):
                    if tok not in dropped_fragments:
                        dropped_fragments.append(tok)
                else:
                    tokens.append(tok)
        add = [t["name"] for t in normalize_tags(tokens, clean_names + keep)
               if t["name"] not in keep]
        remap.append({"record_id": rid, "标题": info["标题"],
                      "remove": info["bad"], "new_tags": keep + add})

    referenced_bad = set()
    for info in referenced.values():
        referenced_bad.update(info["bad"])
    plan = {"remap": remap,
            "delete_options": garbage,
            "delete_unreferenced": [g for g in garbage if g not in referenced_bad]}
    emit(args.action, True, data={
        "options_total": len(opts),
        "garbage_total": len(garbage),
        "garbage_preview": [g[:40] + ("…" if len(g) > 40 else "") for g in garbage],
        "records_to_remap": len(remap),
        "dropped_paragraph_fragments": [d[:30] for d in dropped_fragments],
        "plan": plan,
        "note": "确认后把 data.plan 原样存成 JSON 文件，再运行 clean-tags --apply <文件>"})


def cmd_delete_records(args):
    ctx = Context(args, need_settings=False)
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    if not ids:
        emit(args.action, False, errors=[err("NO_IDS", "--ids 为空")])
    deleted = []
    for rid in ids:
        try:
            rec = ctx.client.get_record(ctx.note_table_id, rid)
            deleted.append({"record_id": rid,
                            "标题": cell_text((rec.get("fields") or {}).get("标题"))})
        except BitableError:
            deleted.append({"record_id": rid, "标题": "（读取失败）"})
    ctx.client.batch_delete(ctx.note_table_id, ids)
    emit(args.action, True, data={"deleted": deleted})


# ---------------------------------------------------------------- main

def main():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--config", help="指定 config.json 路径")

    p = argparse.ArgumentParser(description="小红书笔记发布 skill 后端")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("check-config", parents=[parent])
    sp = sub.add_parser("save-config", parents=[parent])
    sp.add_argument("--table-url", required=True)
    sp.add_argument("--auth-code", required=True)
    sub.add_parser("list-accounts", parents=[parent])
    sub.add_parser("list-tags", parents=[parent])
    sp = sub.add_parser("validate", parents=[parent])
    sp.add_argument("--payload", required=True)
    sp = sub.add_parser("upload", parents=[parent])
    sp.add_argument("--payload", required=True)
    sp = sub.add_parser("probe-media", parents=[parent])
    sp.add_argument("--file")
    sp = sub.add_parser("schema-check", parents=[parent])
    sp.add_argument("--fix", help="修复计划 JSON 文件")
    sub.add_parser("queue-check", parents=[parent])
    sp = sub.add_parser("update-record", parents=[parent])
    sp.add_argument("--record-id", required=True)
    sp.add_argument("--payload", required=True)
    sp = sub.add_parser("clean-tags", parents=[parent])
    sp.add_argument("--apply", help="清理计划 JSON 文件")
    sp = sub.add_parser("delete-records", parents=[parent])
    sp.add_argument("--ids", required=True, help="逗号分隔的 record_id")

    args = p.parse_args()
    handlers = {
        "check-config": cmd_check_config,
        "save-config": cmd_save_config,
        "list-accounts": cmd_list_accounts,
        "list-tags": cmd_list_tags,
        "validate": cmd_validate,
        "upload": cmd_upload,
        "probe-media": cmd_probe_media,
        "schema-check": cmd_schema_check,
        "queue-check": cmd_queue_check,
        "update-record": cmd_update_record,
        "clean-tags": cmd_clean_tags,
        "delete-records": cmd_delete_records,
    }
    try:
        handlers[args.action](args)
    except BitableError as e:
        emit(args.action, False, errors=[err("FEISHU_API_ERROR",
             "飞书接口错误 code=%s: %s" % (e.code, e.msg))])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        emit(args.action, False, errors=[err("SCRIPT_ERROR",
             "%s: %s" % (type(e).__name__, e))])


if __name__ == "__main__":
    main()
