"""证据的 token 预算分配器。

为什么单独成模块：上下文里 L1 原文证据通常占掉绝大部分 token。原先的做法是
「每条证据各自按固定字符数截断」，于是整体没有上限（条数 × 单条上限就是最坏情况），
而且所有证据被同等对待——重排得分高的和几乎不相关的一样长，高分证据没有拿到
它应得的篇幅，低分证据又白占位置。

这里三个纯函数覆盖同一件事的三个环节：

    estimate_tokens   算成本
    split_sentences   找可切的位置
    trim_to_budget    在预算内裁剪，且只切在句子边界
    allocate          按相关性把总预算分给各条，注水式回收用不完的份额

与同包其它模块一样，本模块不 import agentmem 的其它模块，输入输出都是基本类型。

关于 token 估算：不同供应商的分词方式本就不同，这里要的是**预算**而不是计费，
所以不引入分词器依赖，只用一套保守的启发式估算。
"""

from __future__ import annotations

from ._base import truncate

#: 注水迭代轮数上限。每轮至少有一条证据被 demand 封顶而退出待分配集合，
#: 因此正常规模的输入远用不到这个上限；设置它纯粹是为了给浮点运算兜底。
_MAX_ROUNDS = 10

#: 默认的截断标记。与 `_base.truncate` 用同一个标记，让「内容被裁过」
#: 在整份提示词里保持同一种表现形式。
DEFAULT_MARKER = "\n…（已截断）…\n"

#: 中文句末标点。语义明确，出现即断句。
_CN_TERMINATORS = "。！？；"

#: 英文句末标点。单独出现的 ``.`` 可能是小数点或缩写点，必须再看后一个字符。
_EN_TERMINATORS = ".!?"

#: 跟在句末标点后的收尾符号，并入前一句。
#: 不并入的话会切出「」」这种只有引号的碎片，裁剪时这类碎片会被当成完整一句占位。
_CLOSERS = "」』】）〕》”’\"'"


def _is_cjk(char: str) -> bool:
    """是否基本区汉字。生僻扩展区不关心——它们在本项目语料里可以忽略。"""
    return "\u4e00" <= char <= "\u9fff"


def estimate_tokens(text: str) -> int:
    """粗估 token 数，仅用于预算分配，不作为计费依据。

    启发式：CJK 一字约一 token，其余字符四字约一 token。向上取整是刻意的——
    估算偏保守，宁可少放一点内容，也不要让真实请求超出上下文窗口。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def split_sentences(text: str) -> list[str]:
    """按句子边界切分，保留分隔符，拼回去与原文完全相同。

    保留分隔符是为了让 ``"".join(split_sentences(t)) == t`` 恒成立：
    裁剪时按整句取舍，不需要在拼接处再补标点，也不会把换行吃掉。

    边界有三类：中文句末标点、英文句末标点、换行。
    英文标点里只有 ``.`` 有歧义——``12.5 nM``、``IC50 = 3.4 µM`` 里的小数点，
    以及 ``Fig. 3`` 这类缩写点，都不能当句子边界。判定办法是看后一个字符：
    只有后面跟空白或已到字符串末尾，``.`` 才算句末。这条规则认得小数点，
    但认不出缩写（``See Fig. 3`` 会切成 ``See Fig.`` 与 ``3``），
    也漏掉 ``He said "stop." Then`` 这种引号紧贴句号的写法。
    两种偏差的代价都只是切点位置不理想——句子变长或变短，内容本身不会被切坏，
    而收紧规则需要维护缩写表，收益不值这个复杂度。
    """
    sentences: list[str] = []
    start = 0
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        boundary = (
            char == "\n"
            or char in _CN_TERMINATORS
            or (char in _EN_TERMINATORS and (index + 1 >= length or text[index + 1].isspace()))
        )
        if not boundary:
            index += 1
            continue

        # 句末标点后面紧跟的换行与收尾引号并入本句：
        # 否则会切出只含 "\n" 或 "」" 的碎片，它们会在裁剪时被误当作一个完整句子。
        end = index
        while end + 1 < length and (text[end + 1] == "\n" or text[end + 1] in _CLOSERS):
            end += 1
        sentences.append(text[start : end + 1])
        start = end + 1
        index = end + 1

    if start < length:
        sentences.append(text[start:])
    return sentences


def _chars_for_tokens(text: str, tokens: int) -> int:
    """按文本自身的 CJK 占比把 token 预算折算成字符数。

    不能写死「一 token 四字符」：中文语料下一 token 就是一个字，
    按四字符折算会让截断结果超出预算四倍。用整段文本的实测比例折算，
    中英混排时也落在合理区间。
    """
    total = estimate_tokens(text)
    if tokens <= 0 or total <= 0:
        return 0
    return max(1, tokens * len(text) // total)


def trim_to_budget(text: str, token_budget: int, *, marker: str = DEFAULT_MARKER) -> str:
    """把文本压进 token 预算，且只在句子边界上裁剪。

    为什么按句子切：按字符切会把 ``IC50 = 12 nM`` 劈成 ``IC50 = 1``，
    在新药研发这类以数值为核心的领域里，切坏的证据比没有证据更危险——
    模型会拿一个错误的数字当事实。

    为什么保头又保尾：结论常写在末尾，只保头会丢掉最需要的那部分；
    头留约 2/3、尾留约 1/3，与 `_base.truncate` 的取舍直觉保持一致。

    单句本身就超预算时（例如一长段没有标点的表格文本），句子级切分无能为力，
    此时才回退到按字符截断——这是唯一会破坏句子完整性的分支。
    """
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text

    # marker 自己要占位置，先从预算里扣掉，否则「内容 + marker」会超出预算
    available = token_budget - estimate_tokens(marker)
    head_budget = available * 2 // 3
    tail_budget = available - head_budget

    sentences = split_sentences(text)

    head_end = 0
    used = 0
    for sentence in sentences:
        cost = estimate_tokens(sentence)
        if used + cost > head_budget:
            break
        used += cost
        head_end += 1

    if head_end == 0:
        # 首句就放不下，说明存在超预算的单句：句子级切分无能为力，退回字符截断，
        # 至少保住开头的信息。字符数只是按整段文本折算出来的估计——被截的那一段
        # 可能比整体更「中文」，于是实际 token 会偏多；超了就按实测比例再缩一档，
        # 直到确实落在预算内（每次至少少一个字符，必然收敛）。
        chars = _chars_for_tokens(text, token_budget)
        result = truncate(text, chars, marker=marker)
        while chars > 1 and estimate_tokens(result) > token_budget:
            chars = max(1, chars * token_budget // estimate_tokens(result))
            result = truncate(text, chars, marker=marker)
        return result

    tail_start = len(sentences)
    used = 0
    # 头尾不得重叠：句子数很少时 tail_start 会停在 head_end 上，尾段整体为空
    while tail_start > head_end:
        cost = estimate_tokens(sentences[tail_start - 1])
        if used + cost > tail_budget:
            break
        used += cost
        tail_start -= 1

    head = "".join(sentences[:head_end])
    tail = "".join(sentences[tail_start:])
    return head + marker + tail


def _water_fill(weights: list[float], active: list[int], caps: list[int], budget: int) -> list[int]:
    """在 ``active`` 内按权重分配 ``budget``，触顶（用不完）的余量回收再分。

    返回的份额逐条不超过 ``caps``，总和不超过 ``budget``。

    迭代终止条件：每轮先按剩余权重比例试分，凡是试分结果达到自己上限的条目
    就被钉死在上限并从待分集合里移除，省下的预算进入下一轮；当某一轮没有人触顶时，
    把取整丢掉的零头补给权重最高的几条后收工。因为每轮只要有触顶就会至少移除一条，
    待分集合严格缩小，所以最多 ``len(active)`` 轮必然结束——不会出现浮点来回震荡。
    """
    shares = [0] * len(weights)
    pending = list(active)
    remaining = budget
    rounds = max(_MAX_ROUNDS, len(active) + 1)

    for _ in range(rounds):
        if remaining <= 0 or not pending:
            break

        total_weight = sum(max(weights[index], 0.0) for index in pending)
        if total_weight <= 0:
            # 权重全为零（或全为负，被下面的 max 压平）时退化为均分，绝不做除零
            base, extra = divmod(remaining, len(pending))
            proposals = {
                index: base + (1 if position < extra else 0)
                for position, index in enumerate(pending)
            }
        else:
            proposals = {
                index: int(remaining * max(weights[index], 0.0) / total_weight) for index in pending
            }

        topped = [index for index in pending if proposals[index] >= caps[index]]
        if not topped:
            # 没人触顶：这就是最终份额。整数取整会剩几个 token，补给权重最高的几条，
            # 否则预算会被白扔掉（预算浪费等于证据变少）。
            leftover = remaining - sum(proposals.values())
            for index in sorted(pending, key=lambda item: weights[item], reverse=True):
                if leftover <= 0:
                    break
                bonus = min(caps[index] - proposals[index], leftover)
                proposals[index] += bonus
                leftover -= bonus
            for index in pending:
                shares[index] += proposals[index]
            break

        topped_set = set(topped)
        for index in topped:
            shares[index] += caps[index]
            remaining -= caps[index]
        pending = [index for index in pending if index not in topped_set]

    return shares


def allocate(
    weights: list[float],
    total_budget: int,
    *,
    min_tokens: int,
    demands: list[int] | None = None,
    refunds: list[int] | None = None,
) -> list[int]:
    """按权重把 ``total_budget`` 分给各条证据，返回每条分到的 token 数。

    参数：
        weights: 各条的相关性得分（如重排分）。调用方应把「没有分」兜底成 0.0；
            负值按 0 参与比例计算（相关度为负等价于没有相关度），
            但在丢弃排序时仍视为最低。
        demands: 各条**完整内容**需要的 token 数（``estimate_tokens(内容)``）。
            给出它会触发注水：某条分到的份额超过自己实际需要的部分会被回收，
            重新分给还不够的那些。这是本函数最有价值的地方——短证据不该占着
            用不完的配额。传 None 表示不设上限。

    下限规则：份额低于 ``min_tokens`` 的条目整条丢弃（返回 0），而不是塞一个
    没有信息量的残片。这里比字面规则多一个例外——**已经完整放下的条目不算丢弃**：
    一条只需要 60 token 的短证据在 ``min_tokens=120`` 下应该整条保留，
    它的 60 token 全是有效信息，没有「残片」问题；按字面规则把它丢掉反而
    是白白损失证据。因此只有当份额低于下限**且低于自身需求**（即真会被切碎）时才丢。

    丢弃从权重最低的那条开始，一次只丢一条，丢掉后释放的预算重新分配再检查——
    一次丢多条会误伤那些「只是因为别人还没被砍掉才暂时偏低」的条目。

        refunds: 丢掉某条时额外还回预算池的 token（通常是它自己的标签开销）。
            调用方往往先按**全部**候选扣掉每条的固定开销再来分配；若丢掉的条目的
            开销不还回来，候选越多、浪费越多——实测要 12 条证据，只渲染出 6 条
            （比要 8 条时的 7 条还少），2000 的预算里 361 token 白扣在了根本不会
            出现的标签上。传 None 表示不返还（旧行为）。

    返回值长度恒等于 ``weights``，0 表示该条不进上下文，且恒有
    ``sum(返回值) <= total_budget + sum(被丢弃条目的 refunds)``。
    """
    count = len(weights)
    if count == 0:
        return []
    if demands is not None and len(demands) != count:
        raise ValueError("demands 必须与 weights 等长")
    if refunds is not None and len(refunds) != count:
        raise ValueError("refunds 必须与 weights 等长")

    caps = list(demands) if demands is not None else None
    # 没有 demand 约束时，把上限设成整份预算：一条证据最多也只能拿这么多
    effective_caps = caps if caps is not None else [total_budget] * count

    active = list(range(count))
    shares = [0] * count
    # 每轮最多丢一条，丢完即收敛，所以最多 count 轮
    for _ in range(count):
        if not active or total_budget <= 0:
            break
        shares = _water_fill(weights, active, effective_caps, total_budget)
        losers = [
            index
            for index in active
            if shares[index] < min_tokens and (caps is None or shares[index] < caps[index])
        ]
        if not losers:
            break
        dropped = min(losers, key=lambda index: (weights[index], index))
        active.remove(dropped)
        if refunds is not None:
            # 这条不会渲染，它预先扣掉的开销还给还留着的条目
            total_budget += refunds[dropped]

    kept = set(active)
    return [shares[index] if index in kept else 0 for index in range(count)]
