# MemoryFork · Step 1

一条过去有用的订单导入经验，换一个接口版本后会怎样？本实验对同一批合成订单分别执行两种固定策略，观察切换策略后哪笔订单得到修正，哪笔订单出错。

## 在 Windows 上复现

在项目根目录用 PowerShell 运行以下两条命令。实测 Python 3.13.9，只用标准库中的 SQLite 和 unittest，无需安装依赖。

```powershell
.\demo.ps1
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

`data/demo.json` 包含演示订单。`data/cross.json` 交换两个版本对应的币种，供测试独立验证同样的对错变化。`test.ps1` 检查两种策略的逐单金额、元数据和判分，单独改变数据库和期望文件来检验评分器，并连续运行两次演示核对输出。演示中的 FAIL 是订单判分，测试通过的标志是 `Ran 3 tests` 和 `OK`。
