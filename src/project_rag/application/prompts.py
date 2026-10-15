"""
知识库通道的提示词。

**为什么放在 application/ 而不是 adapters/**:提示词是业务规则 —— 该怎么答、
答不出来怎么办、引用怎么标 —— 换一个 LLM 供应商不该动它。
适配器只管「怎么把消息发出去」,不管「发什么」。
"""

from __future__ import annotations

from collections.abc import Sequence

from project_rag.domain import STRATEGY_NAMES, Citation, ConversationTurn, RewriteStrategy

# 每条上下文长这样:编号 + 来源 + 正文。编号必须和 citations 的 index 对齐,
# 否则答案里的 [1] 指不到东西。
_CONTEXT_BLOCK = "[{index}] 来源:{source}\n{text}"

# 历史整块只在**有历史**时才出现:没有历史时提示词必须与不带历史时逐字一致 ——
# 否则「这一轮和上一轮的差别」里混进了一个与历史无关的变量,评估就说不清了。
_HISTORY_BLOCK = "对话历史(最近 {rounds} 轮,只用来理解指代,不要照抄):\n{history}\n\n"

RAG_ANSWER_PROMPT = """你是一个企业知识库问答助手。请**只依据下面的资料**回答用户问题。

{history}要求:
1. 用到资料的地方,在句末标出来源编号,例如 [1] 或 [1][2];
2. 资料里找不到答案时,直接回答「信息不足,无法回答,请联系人工客服」,不要用自己的知识补充;
3. 只输出回答本身,不要复述这些要求,也不要罗列资料原文。

资料:
{context}

问题:{question}

回答:"""


def render_context(citations: Sequence[Citation]) -> str:
    """输入:引用列表。行为:编号拼成上下文块。输出:上下文字符串(空列表给空串)。"""
    return "\n\n".join(
        _CONTEXT_BLOCK.format(
            index=citation.index,
            source=citation.chunk.source,
            # 用 context_text 而不是 text:有父块就给父块(上下文中更全),
            # 没有父块的旧数据自动退回子块本身
            text=citation.chunk.context_text,
        )
        for citation in citations
    )


def render_history(history: Sequence[ConversationTurn]) -> str:
    """输入:历史轮次(时间正序)。行为:渲染成一轮两行(Q/A)。输出:历史文本;空历史给空串。"""
    return "\n".join(f"Q: {turn.question}\nA: {turn.answer}" for turn in history)


def rag_answer_prompt(
    citations: Sequence[Citation],
    question: str,
    history: Sequence[ConversationTurn] = (),
) -> str:
    """输入:引用列表 + 用户问题 + 可选的历史轮次。行为:渲染历史、上下文与提问。输出:完整提示词。"""
    block = (
        _HISTORY_BLOCK.format(rounds=len(history), history=render_history(history))
        if history
        else ""
    )
    return RAG_ANSWER_PROMPT.format(
        history=block, context=render_context(citations), question=question
    )


# ── 查询改写(检索之前那一步)─────────────────────────────────────────────────
#
# **只写「这一段话该改写成什么」,不写「怎么判断该用哪一类」**:判断是分类器
# (微调的 BERT,`tools/lora/`)的活儿。提示词里再写一遍判定规则,就有了两个真值来源
# ——模型改了、提示词没改(或者反过来)时,检索行为会变得没人能解释。
#
# 每一块都用**第二人称祈使句**写成"你要做什么",并要求输出**陈述句/疑问句的检索语句**:
# 改写结果会被直接送进嵌入模型,写成"我可以帮你分析…"这种客服腔会检索到空气。
_REWRITE_INSTRUCTIONS: dict[str, str] = {
    "hypothetical": (
        "用户这个问题太空泛,缺少可以直接检索的实体或术语,照原话去检索召不回东西。\n"
        "请先写一段**假设的答案**:像教材或技术文档里那样,用 3~5 句陈述句把这个话题"
        "讲一遍,把问题里缺的术语、场景、常见叫法都补进去。\n"
        "这段假设答案本身就**当作检索语句**用,所以它必须自带实词、必须是陈述句;"
        "不要出现「可能」「大概」「建议你」这类模糊说法,也不要写成对话口吻。"
    ),
    "subquery": (
        "用户这一句话里塞了**两个及以上各自独立、都要被回答**的问题,混在一起检索"
        "两个都召不准。\n"
        "请把它拆成 2~{max_subqueries} 个独立的子问题,**每个子问题自成一句、可以单独拿去检索**,"
        "并且各自的问法要标准(带上原本的实体与术语)。\n"
        "只是并列或对比、答案本来就是一体的(例如「Java 与 C++ 的区别」)**不要拆**。"
    ),
    "denoise": (
        "用户的话里混进了**不需要被回答**的内容(寒暄、情绪、身份铺垫、「请详细回答」"
        "这类元指令、与问题无关的背景介绍)。\n"
        "请去掉这些噪声,只留下他真正要问的那件事,改写成一个干净的检索语句。\n"
        "**不要新增**用户没问的内容,也不要替他扩展问题 —— 这一步只做减法。"
    ),
}

REWRITE_PROMPT = """你要为检索系统改写一句用户提问。

已经判定好的改写策略:{strategy_name}（{strategy}）
{instruction}

通用要求:
1. 只输出一个 json 对象,形如 {{"queries": ["...", "..."]}},前后不要有任何解释;
2. ``queries`` 的每一项都是**将要拿去检索的句子**,不是对用户问题的回答,也不要写序号;
3. 不要编造用户没提过的具体事实(数字、版本号、公司名、链接);
4. 一律用中文,保留原有的专业术语与英文缩写(如 GIL / JVM / LoRA)。

用户原话:{question}

json:"""


def rewrite_prompt(
    strategy: RewriteStrategy, question: str, *, max_subqueries: int = 3
) -> str:
    """输入:策略 + 用户原话 + 子问题个数上限。行为:渲染改写提示词。输出:提示词全文。

    ``strategy="direct"`` 会**当场抛**:直接检索的定义就是「一个字都不改」,
    根本没有提示词可渲染 —— 那条路上多调一次模型,是白花钱还把改写结果搞脏。
    调用方(``application/query_rewrite.plan_rewrite``)在进这一步之前就该分好支。
    """
    instruction = _REWRITE_INSTRUCTIONS.get(strategy)
    if instruction is None:
        raise ValueError(
            f"策略 {strategy!r} 没有改写提示词;直接检索不该调模型,"
            f"其余请从 {sorted(_REWRITE_INSTRUCTIONS)} 里取"
        )
    return REWRITE_PROMPT.format(
        strategy_name=STRATEGY_NAMES[strategy],
        strategy=strategy,
        # 先渲染指令块自己的占位符,再整块塞进外层的 format —— 值不会被二次解析,
        # 所以指令块里那个 {max_subqueries} 不会和外层的大括号打架
        instruction=instruction.format(max_subqueries=max_subqueries),
        question=question,
    )
