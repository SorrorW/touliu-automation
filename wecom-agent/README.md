# wecom-agent —— 企微智能表格 → 本地代码自动化

从企业微信**智能表格**读取数据，字段映射为参数，执行**本地写好的业务代码**。


## 主号四主品流程（2026-10-19）

抖音主表和旧素材表统一由 `main_account_flow.py` 处理，业务部只保留归属信息。
按商品ID选择鲜会鲜主号现有计划，未绑定时自动申请合作达人授权，生效后加入对应计划。
不再等待人工“允许授权”或21点、23点窗口；此变更不调整现有分部计划的ROI、预算、启停和日报。

完整规则、单条上线、只读预览与故障恢复见 [主号流程说明](../docs/main-account-migration/README.md)。

## 架构

```
企微智能表格(应用创建) ──API──▶ wecom_client.py ──▶ poll_and_run.py
                                                        │  字段映射(FIELD_MAP)
                                                        ▼
                                                    local_tools.py（你的业务代码）
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `config.json` | 配置：corpid/secret/docid/sheet_id（**勿提交，已 gitignore**） |
| `wecom_client.py` | 企微智能表格 API 封装（token 缓存 + 读写记录/字段） |
| `local_tools.py` | ★ **你的业务代码**：加函数 + 登记 ACTIONS/FIELD_MAP |
| `poll_and_run.py` | 轮询主程序：拉记录 → 过滤已处理 → 调本地函数 → 标记完成 |
| `probe.py` | 探针：验证凭据/权限、列出子表、预览记录 |

## 使用

```bash
# 手动触发一次（默认 direct 模式：确定性字段映射直调本地函数）
python poll_and_run.py

# 只查看将处理什么，不执行
python poll_and_run.py --dry-run

# 忽略已处理状态，全部重跑
python poll_and_run.py --force

# Agent 模式：把记录交给本地 Claude Code 理解并调用本地函数
python poll_and_run.py --mode agent
```

### 定时轮询（Windows）

```bash
schtasks /create /tn "wecom_poll" /sc minute /mo 20 /tr "\"C:\路径\投流\runtime\pythonw.exe\" \"C:\路径\wecom-agent\poll_and_run.py\""
（用 pythonw 直跑：无窗口、无 bat/vbs 中间层；创建后务必在任务计划程序里核对动作路径中文正确）
```
或任务计划程序界面添加。建议把脚本部署到**官网服务器**（43.136.19.62）跑，IP 固定更稳。

### 定时投放日报（Windows）

`daily_report_push.py`：按时段扫描三部门消耗/成交/ROI，按 `config.json` 的
`report_webhooks` 分群推送。部门群按重点计划、潜力计划、今日新计划/老计划新素材
（ROI 目标 4.5）、老计划池分类展示；汇总群保持三部门合计模板。失败自动重试 3 轮。
日报只由本脚本的独立计划任务发送，轮询器不参与发送；独立潜力/重点监控播报及预算告警使用
`config.json` 的 `monitor_webhook`，不会改变 `report_webhooks` 日报群；`monitor_suppressed_alert_plan_ids` 可按计划 ID 取消特定计划的预算告警（常态监控行仍保留）。预算告警触发的自动加预算在执行时读取千川页面当前日预算和今日消耗，按“日预算 - 消耗至少 300 元”动态计算，缺口按 100 元向上取整；已达到 300 元或不限预算时跳过。
用管理员 PowerShell 执行 `register_report_tasks_admin.ps1` 注册七个任务：`wecom_report_morning`（08:00）、`wecom_report_10am`（10:00）、`wecom_report_noon`（12:00）、`wecom_report_14pm`（14:00）、`wecom_report_16pm`（16:00）、`wecom_evening_closeout`（17:00）、`wecom_report_night`（22:30）。08:00—17:00 日报明细只展示扫描时当前在投且有消耗的计划；22:30 使用当天全量计划数作分母，但明细只展示有消耗计划。17:00 收口严格先完成日报四群推送，成功后才执行后续计划动作；日报任一群失败时，收口动作不得启动。任务不启用 `StartWhenAvailable`：错过的时段不会在数小时后补跑抢占浏览器；启动器另有 20 分钟迟到保护窗。旧的 `wecom_daily_report`、`wecom_evening_new_check`、`wecom_old_pool_pause` 任务必须停用，避免并发抢浏览器。

```bash
# 手动推一次（真实推送到群）
python daily_report_push.py
# 只扫描+打印，不推送（检查模板用）
python daily_report_push.py --dry-run
```

推送记录写在 `daily_report_state.json`，当天同一时段已推过的群不会重复发；
进程锁会阻止同一时段被重复启动时再次推送。

## 扩展新业务

1. 在 `local_tools.py` 写函数（参数名用英文，如 `customer`）
2. `FIELD_MAP` 加映射：表格列名 → 参数名
3. `ACTIONS` 登记触发字段 → 函数

## 当前状态（2026-08-13 已跑通）

- ✅ 权限链全通：可信域名(xmfssp.site) → 可信IP → 文档API授权 → create_doc/get_sheet/add_fields/add_records/get_records
- ✅ 应用创建了测试智能表格（docid 在 config.json），写入记录 mBC8ca 并成功经 poll_and_run 执行本地函数
- ⚠️ 注意：应用**只能读应用自己创建的表格**；原客户端手工创建的表格（s3_Af0ABgbzAJECN1Zzi0Ge3QyCbqex1）无法被 API 读取

## 待办/建议

- ~~确定正式表格~~ / ~~旧表数据迁移~~ —— 2026-08-13 已完成（抖音 48 条 + 视频号 13 条）
- Agent 模式需本机安装 Claude Code（`claude` 命令）

## 全自动运行（2026-08-13 起）

- **链路**：表格填「待处理」→ 分时段轮询 → 授权申请 → 授权确认 → **旧计划处理** → 夜间建计划（发布后暂停）→ 回写「已投放/已完成」；计划在 08:00 按名单开启。
- **扣费开关**：`ENABLE_PUBLISH=True` 会真实发布计划；`ENABLE_CHASE=False` 时不做建计划后的追投。自动追投和 ROI&gt;10 预算放开由操作台「自动增长」开关控制，默认关闭。
- **模块化断点重试**：授权申请/授权确认/旧计划处理/建计划/追投（视频号：填表）各自独立记断点；
  哪个模块失败下轮只重跑那个模块，**绝不全盘重来**（已成功的建计划/追投不会重复扣费）
- **旧计划处理**（建计划前，`巨量千川/plan_sweep.py`）：总览页按达人搜计划、时间范围近7天，
  行内商品名/商品ID 匹配的计划先做**是否旧计划判定**，再分类处理：
  计划名日期=今天 → 今日新建（撞车/崩溃恢复场景），不删不建，直接作为追投目标；
  旧计划近7天有数据 → 进编辑页加最新视频素材，**不再建新计划**，只对该计划追投（追投锁定 plan_id）；
  旧计划近7天无数据 → **删除**，随后正常建计划；
  计划名无日期且无数据 → 新旧不明一律不动，记 warning 留人工。
  断点续跑时 chase_plan_id 从 `state.json` 的 `step_notes` 恢复，不会追错计划
- **授权等待**：达人未确认授权时**不算失败、不占重试次数**，回写「等待达人接受」，
  每 1 小时自动复查授权状态；一旦两项授权都生效，先回写「**可投放**」再接着建计划/追投；
  申请失效会自动重提一次；等待超 48 小时才标记「失败」人工介入
- **错误分级**：瞬态错误（导航超时/CDP 断连/网络）幂等模块内原地重试一次；
  平台业务拒绝（个人号需认证企业机构号 / 合作码错误）**直接终结回写「失败」**；
  建计划发现勾选框禁用（商品投放权限未生效）→ 自动吊销授权断点、回退授权等待
- **授权判定**：抖音号授权 + **商品投放**授权，两个都生效才算可投放；
  投放授权页同一达人可能有多行（商品投放/直播投放），只认「商品投放」行的状态
- **人工恢复**：把记录进度改回「待处理/等待达人接受/可投放」即可，下轮自动从断点续跑（无需 --force）
- **`--force` 语义**：只额外捞回「失败」记录从断点续跑；**已投放/已完成记录永远不重跑**
- **每步有日志**：`logs/poll_YYYYMMDD.log`（模块级：开始/成功/失败/等待/耗时）
- **卡哪看哪**：`status.json` 实时显示当前记录与模块（等待授权时显示下次复查时间）
- **浏览器互斥**：与投流操作台经文件锁互斥（`browser.lock` / `browser_poll.lock`）
- **定时任务**：pythonw.exe 直跑 `poll_and_run.py`（无窗口、无 bat/vbs 中间层——vbs 曾因子进程不启动卡死 19 小时）。
  注意：`run_poll.bat` 必须全 ASCII（cmd 按 GBK 读 bat，中文注释/路径都会乱码执行失败），
  切换目录用 `cd /d "%~dp0%"` 而不是写死中文路径
- **自动化总开关**：投流操作台「系统 → 自动化」页可一键开关（写 `automation.json`），
  关闭后定时任务每轮直接退出、不动浏览器；该页同时展示实时状态/授权等待列表/今日日志尾部
- **手动重试**：操作台「系统 → 自动化」任务监控表每行有「立即重试」按钮（提前解除授权等待、失败记录自动捞回，从断点续跑）；命令行等价物：`python poll_and_run.py --retry <记录ID>`
- **手动看日志**：双击 `watch_log.bat` 实时滚动今日日志
- 每轮每表最多处理 5 条（`MAX_TASKS_PER_RUN`），防一次性大量扣费
- 离线单测：`python test_retry_logic.py`（验证选择逻辑/断点保留/授权等待/错误分级，不真实执行）

### 浏览器侧配套修复（2026-08-13，投流\巨量千川）

- `auth.py`：每次开**新标签页**执行（旧标签会被 Edge 卡死导航：JS 响应但 goto 永不完成），
  用完自动关闭；`connect_over_cdp` 补 15s 超时（原来裸连会永久挂起）
- `session_prepare.ensure_qianchuan_page`：goto 超时自动关闭僵尸标签换新页（返回新 Page，调用方均已适配）
- `session_prepare.open_fresh_page`（2026-08-14）：所有「新开标签」统一入口，
  开新页前若千川标签积压达到 5 个就从最旧的开始清理（auth/plan_create/plan_sweep/僵尸换页均已接入），
  防止崩溃残留标签越积越多
- `plan_create`：勾选抖音号前检测「禁用」状态，达人无商品推广权限时给出明确报错
