"""投流配置加载：从仓库根目录 config.local.json 读取账号 / 商品 / 达人配置。

首次使用：复制 config.example.json 为 config.local.json，填入你自己的
千川账号、商品 ID 和需要监控的达人名单。config.local.json 已在 .gitignore
中排除，不会随仓库公开。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"


@dataclass(frozen=True)
class TouliuConfig:
    account_by_department: dict[str, str]  # 业务部 -> 千川账号显示名
    account_id_by_name: dict[str, str]     # 千川账号显示名 -> aavid
    aavid_by_department: dict[str, str]    # 业务部 -> aavid
    product_map: dict[str, str]            # 商品名 -> 千川商品 ID
    big_creators: list[str]                # 重点监控达人名单
    creator_departments: dict[str, str]    # 达人 -> 业务部
    audience_template: str | None          # 视频号人群定向模版名（可选）


def load_touliu_config(path: Path | None = None) -> TouliuConfig:
    """加载 config.local.json；缺失时给出中文提示。"""
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path.name}。\n"
            "请先复制 config.example.json 为 config.local.json，"
            "并填入你自己的千川账号、商品 ID 与达人名单。"
        )
    raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))  # utf-8-sig 兼容带 BOM 的编辑器

    departments: dict = raw.get("departments", {})
    if not departments:
        raise ValueError(f"{cfg_path.name} 中缺少 departments 配置，请参考 config.example.json。")

    account_by_department = {d: v["account_name"] for d, v in departments.items()}
    aavid_by_department = {d: str(v["aavid"]) for d, v in departments.items()}
    account_id_by_name = {v["account_name"]: str(v["aavid"]) for v in departments.values()}

    creators = raw.get("monitor_creators", [])
    return TouliuConfig(
        account_by_department=account_by_department,
        account_id_by_name=account_id_by_name,
        aavid_by_department=aavid_by_department,
        product_map={k: str(v) for k, v in raw.get("products", {}).items()},
        big_creators=[c["name"] for c in creators],
        creator_departments={c["name"]: c["department"] for c in creators},
        audience_template=raw.get("defaults", {}).get("audience_template"),
    )


# 允许在包目录内直接运行脚本（python 巨量千川/session_prepare.py）时也能导入本模块
if __name__ != "__main__" and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
