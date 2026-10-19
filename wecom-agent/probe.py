#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业微信 · 智能表格探针 (probe)

作用（一次跑通三件事）：
  1. 验证 corpid/secret 能否获取 access_token
  2. 验证应用是否有目标文档的读取权限（调用「查询子表」接口）
  3. 列出文档下所有子表（sheet_id / 名称 / 类型），并预览每个智能表前几条记录

用法：
  1. 复制 config.json.example 为 config.json，填入 corpid / secret / docid
     - corpid: 企业ID（企微管理后台 → 我的企业 → 企业信息）
     - secret: 自建应用密钥（应用管理 → 自建 → 某应用 → Secret）
     - docid : 智能表格文档URL里的ID，如 https://doc.weixin.qq.com/smartsheet/xxxxxxxx 中的 xxxxxxxx
  2. python probe.py
  3. 从输出中选择目标子表，把 sheet_id 填入 config.json 的 "sheet_id" 字段

仅用 Python 标准库，无需安装任何依赖。
"""
import json
import sys
import urllib.parse
import urllib.request

# Windows 控制台默认 GBK，统一改用 UTF-8 输出，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
CONFIG_FILE = "config.json"
PREVIEW_LIMIT = 3

# 常见错误码提示
ERR_HINT = {
    40013: "corpid 无效：检查企业ID（企微管理后台 → 我的企业 → 企业信息）",
    40001: "secret 无效或 access_token 失效：检查应用 Secret（应用管理 → 自建 → Secret），注意不是其他应用的",
    40014: "access_token 不合法：稍后重试",
    42001: "access_token 过期：脚本会自动重取，重试即可",
    48002: "API 权限不足：① 应用管理里确认应用已开通「文档」相关权限；"
          "② 在目标文档的权限设置中把该自建应用加入「可调用应用」列表；"
          "③ 确认文档已分享/授权给该应用（用应用可见范围内账号的文档）",
    60011: "没有管理权限：检查应用是否有对应文档的管理权限",
    910000: "文档不存在或无权访问：检查 docid 是否正确、文档是否已授权给应用",
}

def http_post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def get_access_token(corpid, secret):
    qs = urllib.parse.urlencode({"corpid": corpid, "corpsecret": secret})
    with urllib.request.urlopen(f"{BASE}/gettoken?{qs}", timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"[错误] 找不到 {CONFIG_FILE}。请先执行：复制 config.json.example 为 config.json 并填写。")
        sys.exit(1)
    missing = [k for k in ("corpid", "secret", "docid") if not cfg.get(k)]
    if missing:
        print(f"[错误] config.json 缺少配置项: {', '.join(missing)}")
        sys.exit(1)
    return cfg

def cell_text(value):
    """从单元格值中提取可读文本（兼容多种字段类型）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        for sub in value.values():
            t = cell_text(sub)
            if t:
                return t
    if isinstance(value, list):
        parts = [cell_text(v) for v in value]
        parts = [p for p in parts if p]
        return ", ".join(parts)
    return ""

def preview_records(token, docid, sheet_id):
    """取前几条记录，返回 (字段名列表, 记录预览行列表, 错误信息)。"""
    r = http_post_json(
        f"{BASE}/wedoc/smartsheet/get_records?access_token={token}",
        {
            "docid": docid,
            "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "limit": PREVIEW_LIMIT,
            "offset": 0,
        },
    )
    if r.get("errcode", 0) != 0:
        return None, None, f"errcode={r.get('errcode')} errmsg={r.get('errmsg')}"
    records = (r.get("data") or {}).get("records", [])
    fields = []
    lines = []
    for rec in records:
        values = rec.get("values", {})
        for fname in values:
            if fname not in fields:
                fields.append(fname)
        parts = []
        for fname in fields:
            parts.append(f"{fname}={cell_text(values.get(fname))}")
        lines.append("#" + str(rec.get("record_id", "")) + "  " + " | ".join(parts))
    return fields, lines, None

def main():
    cfg = load_config()
    print("== 第1步: 获取 access_token ==")
    r = get_access_token(cfg["corpid"], cfg["secret"])
    if r.get("errcode", 0) != 0:
        print(f"[失败] errcode={r.get('errcode')} errmsg={r.get('errmsg')}")
        print(ERR_HINT.get(r.get("errcode"), "未知错误，请对照企微官方错误码文档排查。"))
        sys.exit(1)
    token = r["access_token"]
    print(f"[OK] access_token 获取成功（有效期 {r['expires_in']}s，仅本进程内使用）")

    print("\n== 第2步: 验证文档权限 + 查询子表 ==")
    r = http_post_json(
        f"{BASE}/wedoc/smartsheet/get_sheet?access_token={token}",
        {"docid": cfg["docid"], "need_all_type_sheet": True},
    )
    if r.get("errcode", 0) != 0:
        print(f"[失败] errcode={r.get('errcode')} errmsg={r.get('errmsg')}")
        print(ERR_HINT.get(r.get("errcode"), "未知错误，请对照企微官方错误码文档排查。"))
        sys.exit(1)
    sheets = r.get("sheet_list", [])
    if not sheets:
        print("[失败] 文档下没有任何子表，请确认 docid 是智能表格文档。")
        sys.exit(1)
    print(f"[OK] 权限验证通过，文档下共 {len(sheets)} 个子表：")
    smart_sheets = []
    for s in sheets:
        print(f"  - sheet_id={s.get('sheet_id')}  名称={s.get('title')}  类型={s.get('type')}  可见={s.get('is_visible')}")
        if s.get("type") == "smartsheet":
            smart_sheets.append(s)

    if not smart_sheets:
        print("\n[提示] 没有 type=smartsheet 的子表，请确认该文档是智能表格（而非在线表格/仪表盘/说明页）。")
        sys.exit(1)

    print(f"\n== 第3步: 预览智能表格子表前 {PREVIEW_LIMIT} 条记录 ==")
    for s in smart_sheets:
        sid, title = s.get("sheet_id"), s.get("title")
        try:
            fields, lines, err = preview_records(token, cfg["docid"], sid)
        except Exception as e:
            print(f"  [{title}] 预览失败: {e}")
            continue
        if err:
            print(f"  [{title}] 预览失败: {err}")
            continue
        print(f"  [{title}] (sheet_id={sid})  字段: {', '.join(fields) if fields else '(空)'}")
        for ln in lines:
            print(f"    {ln}")

    print("\n== 结论 ==")
    print("✔ 凭据有效" if True else "")
    print("✔ 文档可读、权限正常")
    print("请从上面的子表列表中确定你的目标表，把其 sheet_id 填入 config.json 的 \"sheet_id\" 字段，")
    print("后续 poll_and_run.py 将用它拉取记录。")

if __name__ == "__main__":
    main()
