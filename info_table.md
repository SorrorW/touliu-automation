# 投流系统 - 信息格式表

> 每个操作所需的输入参数，按模块整理。

---

## 1. 授权达人（auth.py）

```bash
python -m 巨量千川.auth authorize --creator <抖音号ID> --code <合作码> --department <业务部>
```

| 字段 | 示例 | 必填 | 说明 |
|------|------|------|------|
| 抖音号 ID | `12345678A` | ✅ | 达人/合作抖音号 |
| 合作码 | `10000000001` | ✅ | 平台生成的授权合作码 |
| 业务部门 | `业务3部` | ❌ | 默认业务3部 |

```bash
# 简写（仅检查授权状态）
python -m 巨量千川.auth check --creator 12345678A
```

---

## 2. 建投放计划（plan_create.py）

```bash
python -m 巨量千川.plan_create \
  --product <商品名> --creator <抖音号ID> --department <业务部> \
  --creator-name <达人显示名> --seller <业务员姓> \
  [--select-first-video] [--publish]
```

| 字段 | 示例 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| 商品名 | `商品A` | ✅ | — | 商品A/商品B/商品C/商品D |
| 抖音号 ID | `12345678A` | ✅ | — | 要投的达人 |
| 业务部门 | `业务3部` | ✅ | — | 业务1/2/3部 |
| 达人显示名 | `示例达人` | ⚠️* | — | 自动生成计划名时必填 |
| 业务员姓 | `张` | ⚠️* | — | 自动生成计划名时必填 |
| ROI | `7.3` | ❌ | 7.3 | 综合 ROI 目标 |
| 预算 | `300` | ❌ | 300 | 日预算 |
| 选视频 | `--select-first-video` | ❌ | 不选 | 添加第一个可用视频素材 |
| 发布 | `--publish` | ❌ | 不发布 | 直接提交发布计划 |

\* 不传 `--plan-name` 固定名称时，需同时提供 `--creator-name` + `--seller` 自动生成 `MMDD_达人_姓`

---

## 3. 查计划数据（plan_metrics.py）

```bash
# 按计划名查
python -m 巨量千川.plan_metrics plan --plan-name "0718-开心麻麻-赵" --department 业务3部

# 按抖音号查
python -m 巨量千川.plan_metrics plan --creator 12345678A --department 业务3部

# 查追投任务
python -m 巨量千川.plan_metrics chase --creator 12345678A
```

| 字段 | 示例 | 必填 | 说明 |
|------|------|------|------|
| 计划名 | `0718-开心麻麻-赵` | ⚠️ | 与抖音号二选一 |
| 抖音号 ID | `12345678A` | ⚠️ | 与计划名二选一 |
| 业务部门 | `业务3部` | ❌ | 默认业务3部 |

返回：计划名 / 状态 / 消耗 / ROI / 预算 / 投放类型 / 计划ID

---

## 4. 建追投（chase_create.py）

```bash
python -m 巨量千川.chase_create create \
  --plan-id <主计划ID> --budget 100 --hours 24 --department 业务3部
```

| 字段 | 示例 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| 主计划 ID | `3000000000000001` | ⚠️ | — | 与抖音号二选一 |
| 抖音号 | `12345678A` | ⚠️ | — | 与计划ID二选一 |
| 预算 | `100` | ❌ | 100 | 追投金额 |
| 时长 | `24` | ❌ | 24 | 投放小时数 |
| 业务部门 | `业务3部` | ❌ | 业务3部 | — |

返回：task_id

---

## 5. 操作追投（chase_ops.py）

```bash
python -m 巨量千川.chase_ops pause --main-plan-id <主计划ID> --task-id <任务ID> --department 业务3部
python -m 巨量千川.chase_ops resume --main-plan-id <主计划ID> --task-id <任务ID>
python -m 巨量千川.chase_ops budget --main-plan-id <主计划ID> --task-id <任务ID> --amount 200
python -m 巨量千川.chase_ops delete --main-plan-id <主计划ID> --task-id <任务ID>
```

| 字段 | 示例 | 必填 | 说明 |
|------|------|------|------|
| 主计划 ID | `3000000000000001` | ⚠️ | 与抖音号二选一 |
| 抖音号 | `12345678A` | ⚠️ | 与计划ID二选一 |
| 任务 ID | `3000000000000002` | ✅ | 追投任务ID |
| 操作 | `pause` | ✅ | pause/resume/budget/delete |
| 新预算 | `200` | budget操作时必填 | 仅 budget 操作使用 |
| 业务部门 | `业务3部` | ❌ | 默认业务3部 |

---

## 6. 视频号投流（submit_order.py）

```bash
python -m 视频号投流.submit_order --start-date 2026-07-01 --no-budget
```

| 字段 | 示例 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| 开始日期 | `2026-07-01` | ✅ | — | 视频筛选日期 |
| 作者关键词 | `示例作者` | ❌ | 不搜 | 传此参数会搜索作者 |
| ROI | `7.3` | ❌ | 7.3 | 目标 ROI |
| 加热时长 | `24` | ❌ | 24 | 小时数 |
| 人群模版 | `示例模板` | ❌ | 示例模板 | 定向模版名 |
| 跳过预算 | `--no-budget` | ❌ | 关 | 跳过手填预算 |

---

## 完整工作流

```
授权达人 ──→ 建投放计划 ──→ 建追投 ──→ 查数据/操作追投
  auth.py    plan_create.py   chase_create.py  plan_metrics.py
                                               chase_ops.py
```

各步骤串联依赖：

| 步骤 | 产出 | 传给下一步 |
|------|------|-----------|
| auth | 授权生效 | creator + department |
| plan_create | plan_id | plan_id → chase_create |
| chase_create | task_id | task_id → chase_ops |
| plan_metrics | spent/ROI | 监控决策 |
