# MemoryFork · 订单导入与跨会话记忆

一条过去有用的订单导入经验，换一个接口版本后会怎样？本实验对同一批合成订单分别执行两种固定策略，观察切换策略后哪笔订单得到修正，哪笔订单出错。

## 在 Windows 上复现

在项目根目录用 PowerShell 运行。两个演示和测试均使用 Python 3.8+ 标准库与 SQLite。

```powershell
.\demo.ps1
.\demo-memory.ps1
.\test.ps1
```

## 输入和判分

v1 的整数金额以分或美分计，1250 表示 12.50 元或美元。v2 的整数金额以元或美元计，12 表示 12 元或美元。CNY 和 USD 都有两位小数。表中保留订单编号、版本和币种，金额列 `amount_minor` 统一以最小单位存储。

`importer.py` 读取订单文件。`all-major` 把每个输入乘以 100，`all-minor` 原样写入整数。每种策略使用新的 SQLite 内存数据库。`scorer.py` 从数据库取出实际值，按订单编号与独立的 `*.expected.json` 中手工列出的整数逐项比较。缺单和多单也计为 FAIL。

## 演示输出

```text
all-major
order_id version currency actual expected result
D1 v1 CNY 125000 1250 FAIL
D2 v2 USD 1200 1200 PASS
all-minor
order_id version currency actual expected result
D1 v1 CNY 1250 1250 PASS
D2 v2 USD 12 1200 FAIL
```

把 v1 人民币订单放大 100 倍的问题，用“直接写入”可以修正。但将这条经验沿用到 v2 美元订单，就会把 12 美元存成 12 美分。

`data/demo.json` 包含演示订单。`data/cross.json` 交换两个版本对应的币种，供测试独立验证同样的对错变化。第一阶段的 3 个测试检查逐单金额、元数据、独立判分和重复运行。

## Step 2 · 跨进程复用一次成功的导入

会话 A 收到 checkout 项目的 v1/CNY 订单 A1，输入为 1250。每次尝试都调用原有导入器，写入单独的订单库。驱动程序 `demo_memory.py` 在导入完成后打开该库，调用 `scorer.py` 与独立的期望文件比较，并通过标准输入向 A 返回实际金额与 PASS/FAIL。默认策略失败后，A 尝试另一个策略；收到 PASS 时，A 在同一事务中记录成功事件、观察和技能，随后退出。

| 记忆 | 本实验保存的内容 | 生命周期与引用 |
|---|---|---|
| 工作记忆 | 当前项目、订单、检索结果、选定策略 | 当前 Agent 进程内的 `WorkingMemory` |
| 情景记忆 | A 的会话 ID、订单、输入金额、所试策略、实际入库金额和评分 | SQLite 的 `events` 表，每次尝试一条 |
| 语义记忆 | checkout 的 v1/CNY 样例 A1 使用 all-minor 通过校验 | `observations.event_id` 指向通过校验的事件 |
| 技能记忆 | 在相同范围内执行 all-minor 导入 | `skills.observation_id` 指向支持该动作的观察 |

检索沿外键找到来源事件，精确匹配项目、接口版本和币种。订单编号和金额可以变化。三个对照各在新 Python 进程中接收项目、B1 订单文件和记忆库路径，各选一次策略。驱动程序等进程退出后才评分。每轮演示使用独立临时目录，结束时清理数据库。

一次 `.\demo-memory.ps1` 的实际输出如下。PID 和会话 ID 随运行变化。

```text
group pid order lookup action actual expected result provenance
A 40840 A1 miss all-major 125000 1250 FAIL session=508b4292e173415d9ec49b9f21c9ba61 event=1
A 40840 A1 miss all-minor 1250 1250 PASS session=508b4292e173415d9ec49b9f21c9ba61 event=2
learned 40600 B1 hit all-minor 2750 2750 PASS skill=1 observation=1 session=508b4292e173415d9ec49b9f21c9ba61 event=2
empty 41296 B1 miss all-major 275000 2750 FAIL -
other-project 34620 B1 miss all-major 275000 2750 FAIL -
```

`other-project` 使用同一份已学习的记忆库和 B1 订单，仅将项目从 checkout 改为 billing。`.\test.ps1` 依次运行两个阶段，共 5 个测试。第二阶段检查跨进程动作、金额、评分和来源链，逐项改变范围条件验证隔离，并验证连续两次 FAIL 只留下情景记忆。
