#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wecom_client.py —— 企微智能表格 API 封装（正式版，基于已验证的接口调用方式）

依赖：仅 Python 标准库。配置读取 config.json（corpid/secret/docid/sheet_id）。
"""
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_cache.json")

try:
    for _s in (__import__("sys").stdout, __import__("sys").stderr):
        _s.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_config():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
              encoding="utf-8") as f:
        return json.load(f)


def _http_json(url, payload=None):
    if payload is None:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WeComClient:
    """企微智能表格客户端。"""

    def __init__(self, config=None):
        self.cfg = config or _load_config()

    def get_access_token(self, force=False):
        """获取并缓存 access_token（缓存到文件，避免频繁 gettoken）。"""
        cache = {}
        if not force and os.path.exists(TOKEN_CACHE):
            try:
                cache = json.load(open(TOKEN_CACHE, encoding="utf-8"))
            except Exception:
                cache = {}
        if cache.get("token") and cache.get("expire", 0) > time.time() + 120:
            return cache["token"]
        qs = urllib.parse.urlencode({"corpid": self.cfg["corpid"],
                                     "corpsecret": self.cfg["secret"]})
        r = _http_json(f"{BASE}/gettoken?{qs}")
        if r.get("errcode", 0) != 0:
            raise RuntimeError(f"gettoken 失败: {r}")
        cache = {"token": r["access_token"], "expire": time.time() + r["expires_in"] - 300}
        json.dump(cache, open(TOKEN_CACHE, "w", encoding="utf-8"))
        return cache["token"]

    def _post(self, api, payload):
        r = _http_json(f"{BASE}/wedoc/smartsheet/{api}?access_token={self.get_access_token()}",
                       payload)
        if r.get("errcode", 0) != 0:
            raise RuntimeError(f"{api} 失败: errcode={r.get('errcode')} errmsg={r.get('errmsg')}")
        return r

    def get_records(self, offset=0, limit=100, record_ids=None):
        """读取记录。key 用字段标题。返回 records 列表。"""
        payload = {"docid": self.cfg["docid"], "sheet_id": self.cfg["sheet_id"],
                   "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
                   "limit": limit, "offset": offset}
        if record_ids:
            payload["record_ids"] = record_ids
        return self._post("get_records", payload).get("records", [])

    def get_all_records(self):
        """分页拉取全部记录。"""
        out, offset, step = [], 0, 100
        while True:
            recs = self.get_records(offset=offset, limit=step)
            out.extend(recs)
            if len(recs) < step:
                break
            offset += step
        return out

    def get_fields(self):
        """查询字段列表（标题/ID/类型）。"""
        payload = {"docid": self.cfg["docid"], "sheet_id": self.cfg["sheet_id"],
                   "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE"}
        return self._post("get_fields", payload).get("fields", [])

    def add_records(self, records):
        """写入记录。records: [{"values": {字段标题: 值}}]。
        注意：文本字段值必须用 text_cell() 结构化格式 [{"type":"text","text":"..."}]，
        纯字符串虽返回 errcode 0 但实际不落库（get_records 读回为空）。"""
        payload = {"docid": self.cfg["docid"], "sheet_id": self.cfg["sheet_id"],
                   "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE", "records": records}
        return self._post("add_records", payload).get("records", [])

    def update_records(self, records):
        """更新记录（写回结果）。records: [{"record_id": "...", "values": {字段标题: 值}}]。
        文本值同样要用 text_cell() 结构化格式。"""
        payload = {"docid": self.cfg["docid"], "sheet_id": self.cfg["sheet_id"],
                   "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE", "records": records}
        return self._post("update_records", payload).get("records", [])

    # —— 2026-09-07 白名单表格页：子表/字段管理（薄封装，sheet_id 可显式指定）——

    def add_sheet(self, title):
        """在现有 docid 下新建子表，返回新 sheet_id。
        properties.title 尽量一次给对（09-02 改子表名曾撞 2543303 未解）。"""
        payload = {"docid": self.cfg["docid"], "properties": {"title": title}}
        r = self._post("add_sheet", payload)
        return (r.get("properties") or {}).get("sheet_id") or r.get("sheet_id")

    def add_fields(self, fields, sheet_id=None):
        """新增字段（复数形态；单数形态 HTTP 404，09-02 矩阵表实证）。
        fields: [{"field_title":..., "field_type":..., "property":{...}?}]"""
        payload = {"docid": self.cfg["docid"],
                   "sheet_id": sheet_id or self.cfg["sheet_id"], "fields": fields}
        return self._post("add_fields", payload)

    def update_fields(self, fields, sheet_id=None):
        """更新字段（改默认首字段标题用）。fields: [{"field_id":..., "field_title":...}]"""
        payload = {"docid": self.cfg["docid"],
                   "sheet_id": sheet_id or self.cfg["sheet_id"], "fields": fields}
        return self._post("update_fields", payload)


def text_cell(value):
    """把字符串转成企微文本字段的 CellValue 结构化格式（写数据用）。

    返回 [{"type":"text","text":"..."}]；空串返回 None（调用方应跳过该字段）。
    注意：add_records/update_records 传纯字符串不落库，必须用此格式。
    """
    s = "" if value is None else str(value).strip()
    return [{"type": "text", "text": s}] if s else None


def cell_text(value):
    """把单元格值转成可读文本（兼容字符串/数组完整模式）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(cell_text(v) for v in value if v)
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"])
        for k in ("name", "value", "user_id"):
            if k in value:
                return str(value[k])
        return ""
    return str(value)


def simplify_record(record):
    """把企微记录转成 {字段标题: 文本值} 的简单字典。"""
    return {k: cell_text(v) for k, v in record.get("values", {}).items() if cell_text(v)}


if __name__ == "__main__":
    c = WeComClient()
    print("字段:", [(f["field_title"], f["field_type"]) for f in c.get_fields()])
    print("\n记录:")
    for rec in c.get_all_records():
        print(" ", rec["record_id"], simplify_record(rec))
