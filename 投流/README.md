# 投流自动化工具集

通过 CDP 连接本地 Edge，自动操作多平台投放后台的全流程自动化。

## 依赖

- Windows（Edge）
- Python 3.12+
- `pip install "playwright>=1.60,<2" openpyxl`

## 项目结构

```
投流/
│
├── 巨量千川/               # 抖音千川投流
│   ├── __init__.py
│   ├── session_prepare.py  # 浏览器启动 + 部门账号切换  ✅
│   ├── plan_metrics.py     # 查询追投任务消耗和 ROI    ✅（按抖音号搜索已兼容 aurora 新组件）
│   ├── chase_ops.py        # 暂停/恢复/改预算/删除追投  ✅
│   ├── chase_create.py     # 创建追投计划               ✅
│   ├── auth.py             # 抖音号授权 + 投放授权       ✅
│   ├── plan_create.py      # 创建投放计划（72h 素材闸 + 勾选禁用防线）✅
│   ├── plan_sweep.py       # 老计划新素材：按达人×商品找存量计划 → 加素材 → 起投 ✅
│   ├── plan_power.py       # 计划启停底层（_set_plan_power，恢复槽/暂停用）✅
│   ├── commission_gate.py  # 佣金闸：>5% 不投，=5% 可投（次日 00:00 生效）✅
│   ├── lifecycle.py        # 建计划窗口闸门（23:00–07:59 + classification_ready）✅
│   ├── old_plan_pool.py    # 老池计划管理
│   ├── manual_whitelist.py # 人工白名单保护（只读，白名单计划绝不自动动）
│   ├── daily_report.py     # 日报
│   ├── old_creator_retarget.py  # 旧「删旧建新」流程 ⚠️ 已被 plan_sweep 老计划新素材路线取代
│   └── touliu_data.xlsx    # 数据台账
│
├── 视频号投流/             # 微信视频号小店推广
│   ├── __init__.py
│   ├── session_prepare.py   # 浏览器拉起/复用底层（含 pick_page）
│   └── submit_order.py      # 全流程：选视频→填表单→模版→协议  ✅
│
├── wecom-agent/            # 企微智能表格轮询后端（订单表 → 授权/旧计划处理/建计划全自动管线）
│   └── （独立 README，见 wecom-agent/README.md）
│
├── runtime/                # 便携 Python 运行时（所有脚本/后台进程统一用它）
│
├── webui/                  # 操作界面（FastAPI + pywebview 桌面壳）✅
│   ├── app.py               # API 路由（薄层，复用 巨量千川 模块）
│   ├── jobs.py              # 后台任务管理（全局浏览器锁，串行执行）
│   ├── desktop.py           # uvicorn + pywebview 窗口入口
│   └── static/              # 单页前端（无框架无 CDN）
│
├── 启动UI.bat               # 操作人员双击启动界面
├── info_table.md           # 输入参数速查表
├── message_format.md       # 团队消息格式规范
├── 投流任务清单.xlsx         # 任务清单
├── README.md
└── .gitignore
```

调用链（巨量千川）：操作类 → 查询类 → 导航类

---

## 操作界面（webui）

操作人员日常使用入口：**双击桌面「投流操作台」图标**（首次需先运行
`安装桌面图标.ps1` 创建快捷方式），或双击 `启动UI.bat`。
自动打开桌面窗口（无 pywebview 时回退系统浏览器，地址 http://127.0.0.1:8321）。

首次准备（仅需一次）：

```bash
python -m venv .venv
.venv/Scripts/pip install playwright openpyxl fastapi "uvicorn[standard]" pywebview pillow
.venv/Scripts/python -m webui.make_icon   # 生成桌面图标
powershell -ExecutionPolicy Bypass -File 安装桌面图标.ps1
```

界面共 7 页：

| 页面 | 说明 |
|---|---|
| 监控扫描 | 全部门/单部门全表扫描；结果按「全部 / 只达标 / 仅有数据 / 无数据」筛选（不重扫）；每行都可一键追投（固定 100 元/24 小时）；扫描结果自动保存，重开应用恢复展示并显示扫描时间 |
| 建计划 | 填空表单（部门/业务员/抖音号/合作码/商品/达人名/ROI/预算 + 直接发布开关），「粘贴文本自动填写」可用 agent（deepseek/kimi）解析填入；确认后自动执行 授权 → 建投放计划 → 建追投 |
| 重点关注 | 关注的老达人列表（增删 + 一键刷新当日消耗/ROI/成交） |
| 授权管理 | 授权状态检查（抖音号授权/投放授权状态芯片）+ 授权申请 |
| 视频号·投流下单 | 打开视频号会话 + 创建加热订单（选视频→填表→勾协议，不点提交，人工核对后自行提交） |
| 会话/任务 | CDP 状态、切换部门会话、最近任务（含抖音与视频号任务，SQLite 持久化，保留 7 天） |
| 系统日志 | 全局运行日志（抖音/视频号/系统事件），可选行数、可下载 |

数据文件（webui/assets/）：`webui.db`（任务+扫描记录）、`agent_config.json`（LLM 配置）、
`watchlist.json`（关注达人）、`touliu.ico`（桌面图标）、`desktop_error.log`（启动错误日志）。

注意：所有浏览器操作经全局锁串行执行，任务运行期间不要手动操作千川页面；
界面仅监听 127.0.0.1，无登录权限控制，不要对外暴露端口。
数据查询/追投操作/授权检查等不进界面的功能仍可用命令行脚本（见下文）。



---

## 巨量千川 — 完整工作流程

```
输入: 商品 + 抖音号 + 合作码 + 业务部
  │
  ├─ 第1步: auth.py        — 授权检查 → 抖音号授权 + 投放授权
  ├─ 第2步: plan_create.py — 建投放计划
  ├─ 第3步: chase_create.py— 建追投计划
  └─ 第4步: plan_metrics.py + chase_ops.py — 监控轮转 → 查消耗 → 暂停/调预算
```

### 1. session_prepare.py — 准备浏览器会话

拉起 Edge（或复用已有），打开千川并选中指定业务部。

```bash
python 巨量千川/session_prepare.py --department 业务3部
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--department` | 业务1部 / 业务2部 / 业务3部 | 业务3部 |

### 2. plan_metrics.py — 查询追投数据

```bash
# 只查追投任务（推荐）
python 巨量千川/plan_metrics.py chase --creator dou20161012

# 查主计划指标
python 巨量千川/plan_metrics.py plan --creator dou20161012
```

### 3. chase_ops.py — 操作追投任务

```bash
python 巨量千川/chase_ops.py budget --creator dou20161012 --task-id <id> --amount 200
python 巨量千川/chase_ops.py pause  --creator dou20161012 --task-id <id>
python 巨量千川/chase_ops.py resume --creator dou20161012 --task-id <id>
python 巨量千川/chase_ops.py delete --creator dou20161012 --task-id <id>
```

### 4. auth.py — 授权管理 ✅

```bash
python -m 巨量千川.auth check --creator dou20161012
python -m 巨量千川.auth authorize --creator dou20161012 --code 96840444931
# 抖音号授权已生效、只需补投放授权时，--code 可省略
```

两步自动完成（已生效的步骤自动跳过，可选 `--department`）：

1. **抖音号授权**：账户中心 → 添加抖音号（抖音号 ID + 达人合作码，授权类型「合作达人」，180 天），提交即生效
2. **投放授权**：非官方抖音号授权管理 → 申请授权（商品投放权限），提交后需达人 24h 内在抖音 APP 站内信确认才生效

`check` 返回两项授权的状态；均生效时退出码为 0。

### 5. plan_create.py — 建投放计划（适配 2026 新版千川 UI）✅

新版千川 UI 变化：入口从"新建计划"改为"商品乘方"，所有操作在同页面完成，
商品列表使用卡片结构（.card-product-item），选达人改为右侧抽屉（.ovui-drawer）。

```bash
# 完整三模块：商品 + 达人 + 视频，填 ROI 7.3/预算 300/计划名
python -m 巨量千川.plan_create \
    --product 肥牛 --creator 20161121Z --department 业务3部 \
    --creator-name 煮妇小承妈 --seller 赵 \
    --select-first-video

# 自动发布
python -m 巨量千川.plan_create \
    --product 肥牛 --creator 20161121Z --department 业务3部 \
    --creator-name 煮妇小承妈 --seller 赵 \
    --select-first-video --publish
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--product` | 商品名称（肥牛/仔骨/上脑/板腱） | 必填 |
| `--creator` | 抖音号/达人 ID | 必填 |
| `--department` | 业务1部 / 业务2部 / 业务3部 | 必填 |
| `--roi` | 综合 ROI 目标 | 7.3 |
| `--budget` | 日预算 | 300 |
| `--creator-name` | 达人显示名，用于生成计划名（如 煮妇小承妈） | 自动生成时必填 |
| `--seller` | 业务员姓氏，用于生成计划名（如 赵） | 自动生成时必填 |
| `--plan-name` | 固定计划名称，覆盖自动生成 `MMDD_达人_姓` | 无 |
| `--select-first-video` | 自动添加第一个可用视频素材 | 关 |
| `--publish` | 是否点击「发布计划」 | 关 |
| `--confirm-risky` | 自动确认平台风险弹窗 | 关 |

### 6. chase_create.py — 建追投

```bash
python 巨量千川/chase_create.py create --creator dou20161012 --plan-id <plan_id> --budget 100 --hours 24
```

### 7. plan_sweep.py — 老计划新素材（2026-09 起替代删旧建新）✅

用户口径：**有计划就走老计划新素材，不删计划**。按「达人 × 商品」在总览页搜索存量
计划（含已暂停——已暂停计划同样占「重复计划」坑，新建会被平台拦截），命中后进入编辑页
追加 ≤72 小时的挂车视频素材（过闸则报「待业务员补新素材」），必要时改名/重置 ROI 后起投。

```bash
# 干跑（只匹配不动作）
python -m 巨量千川.plan_sweep --creator 25656170876 --product 肥牛 --department 业务2部

# 实际加素材（仅素材，不做启停）
python -m 巨量千川.plan_sweep --creator 25656170876 --product 肥牛 \
    --department 业务2部 --apply --materials-only
```

已知限制与口径：
- **sweep 搜索固定近 7 天窗口**，超老的暂停计划扫不到——手动处理时用 `plan_metrics._search_plans(by_creator=True)` 不带时间窗自写驱动。
- 素材 72 小时硬闸：无 ≤72h 挂车视频即失败并给出最新素材年龄。手动补建/补素材进程可用
  环境变量 `TOULIU_ALLOW_STALE_ONCE=1` 一次性破例（按素材时间新→旧放行全部候选），**仅限人工明确拍板后**，正常轮询永不带此变量。
- 「加素材返回空」可能是该达人唯一视频已在计划素材内——直接恢复投放即可，无需加。

### 8. old_creator_retarget.py — 老达人新发视频（⚠️ 已停用，保留参考）

按旧计划名称找到旧计划，进入编辑页确认「部门、业务员、抖音号、商品 ID」，
删除旧计划后新建商品乘方计划（自动选第一个新视频），并对新计划创建素材追投。

```bash
# 只查看旧计划信息，不删除
python -m 巨量千川.old_creator_retarget --old-plan-name "0727_煮妇小承妈_赵"

# 删除旧计划并新建计划 + 追投（默认不发布新计划）
python -m 巨量千川.old_creator_retarget --old-plan-name "0727_煮妇小承妈_赵" --confirm-delete

# 删除旧计划、发布新计划、创建追投
python -m 巨量千川.old_creator_retarget \
    --old-plan-name "0727_煮妇小承妈_赵" \
    --confirm-delete --publish --confirm-risky
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--old-plan-name` | 旧计划名称 | 必填 |
| `--department` | 业务1部 / 业务2部 / 业务3部 | 业务3部 |
| `--publish` | 是否直接发布新计划 | 关 |
| `--confirm-risky` | 自动确认平台风险弹窗 | 关 |
| `--confirm-delete` | 确认删除旧计划；不传则只输出提取信息 | 关 |
| `--chase-budget` | 追投预算 | 100 |
| `--chase-hours` | 追投时长（小时） | 24 |
| `--creator-name` | 达人显示名兜底 | 无 |
| `--seller` | 业务员姓氏兜底 | 无 |
| `--plan-name` | 新计划名称覆盖 | 无 |

---

## 视频号投流

### 准备工作
先确认 Edge 已登录微信小店后台（小店推广 > 加热首页）。

### submit_order.py — 全流程（选视频 → 填表单 → 模版 → 协议，不点提交）

```bash
# 日常用法：不搜作者（避免限流），用默认作者视频，选日期 + 第一条视频
python -m 视频号投流.submit_order --start-date 2026-07-01 --no-budget

# 搜指定作者
python -m 视频号投流.submit_order --keyword 我家厨房1688 --start-date 2026-07-01
```

流程：打开视频弹窗（自动弹或点「添加视频」）→ 清空已选 → 传 --keyword 则添加其他作者搜索
→ 设日期 → 选第一条视频 → 点完成 → 商品ROI → 控成本加热 → ROI 7.3 →
预算 300（选商品ROI后页面自动带出）→ 24小时 → 肥牛片模版 → 勾协议。
停在提交前，人工确认后手动点提交。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--start-date` | 视频筛选日期 YYYY-MM-DD | 无 |
| `--keyword` | 作者关键词（不传则跳过搜索） | 无 |
| `--roi` | 目标 ROI | 7.3 |
| `--no-budget` | 跳过预算手填（自动带 300） | 关 |
| `--hours` | 加热时长 | 24 |
| `--template` | 人群定向模版名 | 肥牛片 |
| `--no-template` | 跳过模版选择 | 关 |

### session_prepare.py — 准备浏览器会话（仅打开页面）

通常不需单独用。

```bash
python 视频号投流/session_prepare.py
```

---

## 注意事项

- 所有操作通过 CDP 连接本地 Edge（端口 **9223**，勿与旧文档 9222 混淆）
- Edge 需先登录对应平台，脚本会自动处理；**不得重启/关闭用户的 Edge，第一页是人工页不动，其余标签页 ≤7**
- 巨量千川访问总览页必须携带 `?aavid=<账号ID>`（一部 1859511296951635、二部 1861049584106636、三部 1861049594712072）
- 搜抖音号时自动从"计划名称"切换到"抖音号"搜索模式；平台 2026-10 改版为 aurora 组件后 `_switch_search_mode` 已双路径兼容（按 placeholder 判定当前模式）
- **建计划窗口**：正常轮询只在 23:00–07:59 建计划（`classification_ready=true` 闸门）；白天补建需 touch `wecom-agent/force_plan_create_window` 标记，用完即删
- **勾选禁用三道防线**（plan_create）：行内/popover 读真实原因 → 噪音过滤（纯数字/%/<6 字符丢弃）→ 含「已存在」走收养、读不出原因落「待业务员核查」——宁可标核查，绝不猜「无推广权限」
- **佣金闸**：佣金率 >5% 不投，=5% 可投；佣金修改次日 00:00 生效
- **人工白名单**（`manual_creator_whitelist.json` + 企微表启用/停用）只读不自动改，命中白名单的计划绝不自动启停
