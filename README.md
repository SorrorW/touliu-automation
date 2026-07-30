# 投流自动化工具集（touliu-automation）

通过 CDP 连接本地 QQBrowser，自动操作多平台投放后台（巨量千川 / 微信视频号）的全流程自动化工具集：达人授权、建投放计划、数据查询、素材追投管理，以及给操作人员用的桌面操作台。

> Automate multi-platform ad-buying backends (Juliang Qianchuan / WeChat Channels) through a local browser over CDP: authorization, plan creation, metrics, chase-task management, plus a desktop operator UI.

## 功能

- **巨量千川**：部门账号切换、达人授权（抖音号/投放）、创建投放计划、素材追投（建/停/改预算/删）、消耗与 ROI 查询、老达人新发视频删旧建新
- **视频号投流**：选视频 → 填表单 → 选人群模板 → 勾协议，停在提交前由人工确认
- **投流操作台（webui）**：FastAPI + pywebview 桌面窗口。监控扫描、建计划表单（粘贴文本可经 LLM 解析自动填写）、重点关注、授权管理、任务记录

## 环境要求

- Windows + QQBrowser（默认路径 `D:\QQBrowser\QQBrowser.exe`，见 `巨量千川/session_prepare.py` 顶部常量）
- Python 3.12+
- `pip install -r requirements.txt`

## 快速开始

1. 安装依赖（建议虚拟环境）：

   ```bash
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt
   ```

2. 配置账号：复制 `config.example.json` 为 `config.local.json`，填入你自己的部门 / 千川账号（aavid）、商品 ID、重点关注达人与视频号人群模板。`config.local.json` 已被 .gitignore 排除，不会泄露。

3. 启动 QQBrowser 并登录对应平台后台（千川 / 视频号小店推广），脚本通过 CDP 端口 9222 接管浏览器。

4. 命令行用法见下文各模块说明；桌面操作台双击 `启动UI.bat`。

## 免责声明

- 本项目仅供学习与技术交流，请仅用于你已获得合法授权的账号和业务场景。
- 自动化操作第三方平台可能违反其服务条款，由此产生的一切后果（包括但不限于账号限制、资金损失）由使用者自行承担。
- 作者不对任何直接或间接损失负责，使用本项目即视为同意以上条款。

## License

[MIT](LICENSE)

## 项目结构

```
touliu-automation/
│
├── 巨量千川/               # 抖音千川投流
│   ├── __init__.py
│   ├── session_prepare.py  # 浏览器启动 + 部门账号切换  ✅
│   ├── plan_metrics.py     # 查询追投任务消耗和 ROI    ✅
│   ├── chase_ops.py        # 暂停/恢复/改预算/删除追投  ✅
│   ├── chase_create.py     # 创建追投计划               ✅
│   ├── auth.py             # 抖音号授权 + 投放授权       ✅
│   ├── plan_create.py      # 创建投放计划               ✅
│   └── old_creator_retarget.py  # 老达人新发视频：删旧建新 + 追投  ✅
│
├── 视频号投流/             # 微信视频号小店推广
│   ├── __init__.py
│   ├── session_prepare.py   # 浏览器拉起/复用底层（含 pick_page）
│   └── submit_order.py      # 全流程：选视频→填表单→模版→协议  ✅
│
├── webui/                  # 操作界面（FastAPI + pywebview 桌面壳）✅
│   ├── app.py               # API 路由（薄层，复用 巨量千川 模块）
│   ├── jobs.py              # 后台任务管理（全局浏览器锁，串行执行）
│   ├── desktop.py           # uvicorn + pywebview 窗口入口
│   └── static/              # 单页前端（无框架无 CDN）
│
├── touliu_config.py        # 配置加载（读 config.local.json）
├── config.example.json     # 配置模板（复制为 config.local.json 后填自己的）
├── requirements.txt
├── 启动UI.bat               # 操作人员双击启动界面
├── info_table.md           # 输入参数速查表
├── message_format.md       # 团队消息格式规范
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
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m webui.make_icon   # 生成桌面图标
powershell -ExecutionPolicy Bypass -File 安装桌面图标.ps1
```

> 别忘了先按「快速开始」第 2 步准备好 `config.local.json`。

界面共 5 页：

| 页面 | 说明 |
|---|---|
| 监控扫描 | 全部门/单部门全表扫描；结果按「全部 / 只达标 / 仅有数据 / 无数据」筛选（不重扫）；每行都可一键追投（固定 100 元/24 小时）；扫描结果自动保存，重开应用恢复展示并显示扫描时间 |
| 建计划 | 填空表单（部门/业务员/抖音号/合作码/商品/达人名/ROI/预算 + 直接发布开关），「粘贴文本自动填写」可用 agent（deepseek/kimi）解析填入；确认后自动执行 授权 → 建投放计划 → 建追投 |
| 重点关注 | 关注的老达人列表（增删 + 一键刷新当日消耗/ROI/成交） |
| 授权管理 | 授权状态检查（抖音号授权/投放授权状态芯片）+ 授权申请 |
| 会话/系统 | CDP 状态、切换部门会话、最近任务（SQLite 持久化，保留 7 天） |

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

拉起 QQBrowser（或复用已有），打开千川并选中指定业务部。

```bash
python 巨量千川/session_prepare.py --department 业务3部
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--department` | 业务1部 / 业务2部 / 业务3部 | 业务3部 |

### 2. plan_metrics.py — 查询追投数据

```bash
# 只查追投任务（推荐）
python 巨量千川/plan_metrics.py chase --creator dou12345678

# 查主计划指标
python 巨量千川/plan_metrics.py plan --creator dou12345678
```

### 3. chase_ops.py — 操作追投任务

```bash
python 巨量千川/chase_ops.py budget --creator dou12345678 --task-id <id> --amount 200
python 巨量千川/chase_ops.py pause  --creator dou12345678 --task-id <id>
python 巨量千川/chase_ops.py resume --creator dou12345678 --task-id <id>
python 巨量千川/chase_ops.py delete --creator dou12345678 --task-id <id>
```

### 4. auth.py — 授权管理 ✅

```bash
python -m 巨量千川.auth check --creator dou12345678
python -m 巨量千川.auth authorize --creator dou12345678 --code 10000000001
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
    --product 商品A --creator 12345678A --department 业务3部 \
    --creator-name 示例达人 --seller 赵 \
    --select-first-video

# 自动发布
python -m 巨量千川.plan_create \
    --product 商品A --creator 12345678A --department 业务3部 \
    --creator-name 示例达人 --seller 赵 \
    --select-first-video --publish
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--product` | 商品名称（商品A/商品B/商品C/商品D） | 必填 |
| `--creator` | 抖音号/达人 ID | 必填 |
| `--department` | 业务1部 / 业务2部 / 业务3部 | 必填 |
| `--roi` | 综合 ROI 目标 | 7.3 |
| `--budget` | 日预算 | 300 |
| `--creator-name` | 达人显示名，用于生成计划名（如 示例达人） | 自动生成时必填 |
| `--seller` | 业务员姓氏，用于生成计划名（如 张） | 自动生成时必填 |
| `--plan-name` | 固定计划名称，覆盖自动生成 `MMDD_达人_姓` | 无 |
| `--select-first-video` | 自动添加第一个可用视频素材 | 关 |
| `--publish` | 是否点击「发布计划」 | 关 |
| `--confirm-risky` | 自动确认平台风险弹窗 | 关 |

### 6. chase_create.py — 建追投

```bash
python 巨量千川/chase_create.py create --creator dou12345678 --plan-id <plan_id> --budget 100 --hours 24
```

### 7. old_creator_retarget.py — 老达人新发视频

按旧计划名称找到旧计划，进入编辑页确认「部门、业务员、抖音号、商品 ID」，
删除旧计划后新建商品乘方计划（自动选第一个新视频），并对新计划创建素材追投。

```bash
# 只查看旧计划信息，不删除
python -m 巨量千川.old_creator_retarget --old-plan-name "0727_示例达人_张"

# 删除旧计划并新建计划 + 追投（默认不发布新计划）
python -m 巨量千川.old_creator_retarget --old-plan-name "0727_示例达人_张" --confirm-delete

# 删除旧计划、发布新计划、创建追投
python -m 巨量千川.old_creator_retarget \
    --old-plan-name "0727_示例达人_张" \
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
先确认 QQBrowser 已登录微信小店后台（小店推广 > 加热首页）。

### submit_order.py — 全流程（选视频 → 填表单 → 模版 → 协议，不点提交）

```bash
# 日常用法：不搜作者（避免限流），用默认作者视频，选日期 + 第一条视频
python -m 视频号投流.submit_order --start-date 2026-07-01 --no-budget

# 搜指定作者
python -m 视频号投流.submit_order --keyword 示例作者 --start-date 2026-07-01
```

流程：打开视频弹窗（自动弹或点「添加视频」）→ 清空已选 → 传 --keyword 则添加其他作者搜索
→ 设日期 → 选第一条视频 → 点完成 → 商品ROI → 控成本加热 → ROI 7.3 →
预算 300（选商品ROI后页面自动带出）→ 24小时 → 示例模板模版 → 勾协议。
停在提交前，人工确认后手动点提交。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--start-date` | 视频筛选日期 YYYY-MM-DD | 无 |
| `--keyword` | 作者关键词（不传则跳过搜索） | 无 |
| `--roi` | 目标 ROI | 7.3 |
| `--no-budget` | 跳过预算手填（自动带 300） | 关 |
| `--hours` | 加热时长 | 24 |
| `--template` | 人群定向模版名 | 示例模板 |
| `--no-template` | 跳过模版选择 | 关 |

### session_prepare.py — 准备浏览器会话（仅打开页面）

通常不需单独用。

```bash
python 视频号投流/session_prepare.py
```

---

## 注意事项

- 所有操作通过 CDP 连接本地 QQBrowser（端口 9222）
- QQBrowser 需先登录对应平台，脚本会自动处理
- 巨量千川访问总览页必须携带 `?aavid=<账号ID>`
- 搜抖音号时自动从"计划名称"切换到"抖音号"搜索模式
