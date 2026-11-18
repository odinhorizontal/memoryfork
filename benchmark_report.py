"""Reduce the public execution ledger into the paired benchmark report."""

from collections import Counter


NAMES = {"A": "无记忆", "B": "平铺检索", "C": "分层记忆", "D": "分层＋自进化"}


def ratio(n, d):
    return f"{n}/{d}（{100*n/d:g}%）" if d else "0/0（无命中）"


def metrics(rows, method):
    blind = [r for r in rows if r["task"] == "blind" and r["method"] == method]
    old = [r for r in rows if r["task"] == "replay" and r["method"] == method]
    hits = [r for r in blind if r["source"]]
    return (sum(r["verdict"] == "PASS" for r in blind), len(blind),
            sum(r["verdict"] == "PASS" for r in old), len(old),
            sum(r["verdict"] == "FAIL" for r in hits), len(hits))


def compare(first, second):
    a, b = first["verdict"] == "PASS", second["verdict"] == "PASS"
    return "胜" if a > b else "负" if a < b else "平"


def example(rows, pair):
    b, d = pair["B"], pair["D"]
    config = b["config"]
    history = [r for r in rows if r["method"] == "shared" and r["config"] == config]
    source = next(r for r in history if r["event"] == b["source"]["event_id"])
    probe = next(r for r in history if r["task"] == "probe" and r["verdict"] == "PASS")
    order = b["order"]
    return (f"配置 {config} 的 H1 为 {order['version']}/{order['currency']}，输入 {order['amount']}。"
            f"B 在两个各匹配一个字段的成功训练事件中取较新的 {source['order']['order_id']}"
            f"（事件 {source['event']}），沿用 {b['action']}，得到 {b['actual']} 分。"
            f"探针 {probe['order']['version']}/{probe['order']['currency']} 的 {probe['action']} 通过，"
            f"D 采用经验证的 {d['source'].get('axis', '精确')} 规则，选 {d['action']}，"
            f"得到 {d['actual']} 分。期望 {d['expected']} 分，B/D 分别为 {b['verdict']}/{d['verdict']}。")


def render(rows, seconds, sha):
    lines = ["# MemoryFork v0.1 四组盲测", "", f"协议与实现冻结于 `{sha}`。"
             "每配置一票，2 个开发配置复现、6 个新增。所有金额以最小货币单位判分。",
             "", "| 方法 | 未见成功数 / 总数 | 旧样例保留率 | 未见错误复用 / 实际采用 |",
             "|---|---:|---:|---:|"]
    rates = {}
    for method, name in NAMES.items():
        ok, n, kept, old, wrong, used = metrics(rows, method)
        rates[method] = (ok/n, wrong/used if used else None)
        reuse = "不适用" if method == "A" else ratio(wrong, used)
        lines.append(f"| {method} {name} | {ratio(ok, n)} | {ratio(kept, old)} | {reuse} |")
    delta = 100 * (rates["D"][0] - rates["B"][0])
    br, dr = rates["B"][1], rates["D"][1]
    lowered = br is not None and dr is not None and dr < br
    lines.extend(["", f"D 相对 B 的未见成功率差为 {delta:+g} 个百分点，"
                  f"至少提高 10 个百分点的目标{'达成' if delta >= 10 else '未达成'}。"
                  f"错误复用率严格降低的目标{'达成' if lowered else '未达成'}。",
                  "", "## 每个配置的配对判分", "",
                  "| 配置 | 来源 | H1 | 期望 | A | B | C | D | D 对 B | D 对 C |",
                  "|---|---|---|---:|---|---|---|---|---|---|"])
    pairs = {}
    for r in rows:
        if r["task"] == "blind":
            pairs.setdefault(r["config"], {})[r["method"]] = r
    counts = {"B": Counter(), "C": Counter()}
    examples = {}
    for config, pair in pairs.items():
        d = pair["D"]
        labels = [compare(d, pair[m]) for m in counts]
        for m, label in zip(counts, labels):
            counts[m][label] += 1
        examples.setdefault(labels[0] == "平", pair)
        grades = " | ".join(pair[m]["verdict"] for m in NAMES)
        origin = {"01": "演示 A", "03": "演示 B"}.get(config, "新增")
        order = d["order"]
        lines.append(f"| {config} | {origin} | {order['version']}/{order['currency']} | "
                     f"{d['expected']} | {grades} | {' | '.join(labels)} |")
    lines.append("")
    for method, tally in counts.items():
        lines.extend([f"D 对 {method}：{tally['胜']} 胜、{tally['平']} 平、{tally['负']} 负。", ""])
    lines.extend(["## 事件解释", ""])
    for same, heading in [(False, "改变判分"), (True, "判分相同")]:
        detail = example(rows, examples[same]) if same in examples else "本轮没有此类配置。"
        lines.extend([f"**{heading}**　{detail}", ""])
    phases = Counter(r["task"] for r in rows)
    probes = len({(r["config"], r["order"]["order_id"]) for r in rows if r["task"] == "probe"})
    lines.extend(["## 资源与边界", "",
                  "| 探针订单 | 初始导入 | 探针导入 | 盲测导入 | 保留回放导入 | 总导入 | 模型调用 | 实测墙钟 |",
                  "|---:|---:|---:|---:|---:|---:|---:|---:|",
                  f"| {probes} | {phases['train']} | {phases['probe']} | {phases['blind']} | "
                  f"{phases['replay']} | {len(rows)} | 0 | {seconds:.2f} s |", "",
                  "墙钟含生成证据、建库、启动会话、独立评分与冻结库检查。"
                  "仅有初始支持样例时，每配置增加 1 个探针、2 次导入；这些记录供四组共用，"
                  "D 相对同证据 B/C 的额外导入为 0。", "",
                  "主实验测量相同证据下规则修订的价值。主动选单与随机选单的相对效果尚待单独比较。"
                  "结果覆盖此合成订单环境、两个候选字段和两种导入动作。", "",
                  "逐次执行见 [trials.jsonl](trials.jsonl)，冻结库前后摘要见 [hashes.json](hashes.json)。"])
    return "\n".join(lines) + "\n"
