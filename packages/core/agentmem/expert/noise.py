"""评测噪声：A/B 与留一法共用的判定阈值。

单独成模块是因为进化闭环（`evolve/cycle.py`）与评测执行器（`expert/evaluator.py`）
都要用它，而前者依赖后者，放在任何一边都会形成循环导入。
"""

from __future__ import annotations

import math

#: A/A（同配置跑两遍）下**逐题配对差值**的标准差，0~100 分制。
#:
#: 实测：先是 8 题评测集上三次 A/A 共 21 个配对样本（标准差 14.9），
#: 之后扩到 29 题又测了一次（13.2），合计 50 个样本的合并标准差约 13.9，
#: 常量取 15 略作保守。（另有一次 29 题 A/A 测得 19.9，但那一轮本机缺了
#: sentence-transformers、检索退化成纯全文，不是同一个系统，已剔除。）
#: 按此阈值，8 题时三次 A/A 的配对总分差（-2.67 / +6.0 / -7.25）
#: 全部被正确判为「无差异」；而原来的 1.0 会把它们
#: **3 次全部**误判为「有显著差异」——同配置对比的假阳性率 100%。
#:
#: ⚠️ 这个值**曾经被写成 32**，而那是在评测里还有两条「假零分」路径时测的：
#: 裁判漏写汇总字段 `score` 时被默认成 0 分、单题抛异常（限流）也记 0 分，
#: 而逐题差值又不看 `measured`——一边占位 0 分、一边测出 97 分，就凭空多出 ±97。
#: 当时的逐题差值 ``[-8,+94,-25,+25,0,+3,+97,+3]`` 里的大摆幅几乎全是这么来的。
#: 用被污染的 32 定阈值，8 题时要求 22.6 分的差距才肯判决，进化闭环会拒绝晋升
#: 真正有用的经验——从「由噪声驱动」矫枉过正成了「什么都不敢判」。
EVAL_PAIRED_DIFF_SD = 15.0


#: 判定 A/B「提升」所需的最小分差 = 2 × 配对均差的标准误 = 2 × SD / √N。
#:
#: 这里**不能**用一个固定的小常数：原本写的是 `MIN_EVAL_DELTA = 1.0`，
#: 几乎任何一次运行都会被判为「有显著变化」。按 2 倍标准误取阈值：
#: 8 题 → 10.6 分，29 题 → 5.6 分，50 题 → 4.2 分。
def min_eval_delta(item_count: int) -> float:
    """给定题量，返回可信的最小分差阈值。"""
    if item_count <= 0:
        return 100.0  # 一题都没有：任何结论都不可信
    return float(round(2 * EVAL_PAIRED_DIFF_SD / math.sqrt(item_count), 2))


def t_critical(df: int) -> float:
    """双侧 95% 的 t 临界值近似：``1.96 + 2.4 / df``。

    与查表值相比：df=7 → 2.30（真值 2.36），df=28 → 2.05（2.05），df→∞ → 1.96。
    题量小时要求更高的 t，免得几道题的偶然一致被读成结论。
    """
    return 1.96 + 2.4 / df if df > 0 else float("inf")


class PairedTest:
    """一组配对差值的显著性检验结果。"""

    __slots__ = ("mean", "n", "significant", "std_error", "t")

    def __init__(
        self,
        n: int,
        mean: float,
        std_error: float | None,
        t: float | None,
        significant: bool,
    ) -> None:
        self.n = n
        self.mean = mean
        self.std_error = std_error
        self.t = t
        self.significant = significant


def paired_test(deltas: list[float], *, sd_floor: float = EVAL_PAIRED_DIFF_SD) -> PairedTest:
    """对配对差值做 t 检验。

    标准差取「本次实测」与 ``sd_floor`` 中的较大者：题少时样本标准差本身很不稳，
    碰巧偏小就会让噪声冒充信号——这正是「拿单次 A/A 当噪声底」犯过的错。
    评测分（0~100）用标定值 :data:`EVAL_PAIRED_DIFF_SD` 兜底；检索指标（0~1）
    没有标定值，传 ``sd_floor=0`` 只用实测。

    界面上的涨跌配色、进化闭环的晋升/淘汰都走这一个函数，两边口径必须一致。
    """
    n = len(deltas)
    if n == 0:
        return PairedTest(0, 0.0, None, None, False)
    mean = sum(deltas) / n
    if n < 2:
        return PairedTest(n, mean, None, None, False)
    variance = sum((x - mean) ** 2 for x in deltas) / (n - 1)
    sd = max(math.sqrt(variance), sd_floor)
    if sd == 0:
        # 所有差值完全相同且没有兜底：没有波动可言，只要不是 0 就算显著
        return PairedTest(n, mean, 0.0, None, mean != 0)
    std_error = sd / math.sqrt(n)
    t = mean / std_error
    return PairedTest(n, mean, std_error, t, abs(t) >= t_critical(n - 1))
