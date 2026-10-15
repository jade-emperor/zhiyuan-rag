"""rewrite 子命令:只看**查询改写**这一步 —— 判成哪一类、改写成了哪几句。

为什么值得单独一个命令:改写出问题时的形状特别难查 —— 它**不报错**。
「分类器没装好」表现成"改写退化为原样检索","模型没按格式吐 json"表现成
"改写结果不可用",两者在问答链路上都只是"召回稍微差一点"。没有这个命令,
唯一的观察窗口是翻日志里那几行 INFO。

它跟 rag / qa 的关系:那两个命令跑的是**结果**,这个命令跑的是**原因那一半** ——
只做判定与改写,**不检索、不生成**。

三条口径:

- 退出码 **0 = 这一步走通了**(哪怕结论是"原样检索"),**1 = 配置 / 装配有问题**。
  判成 0 类不是错误,它是四类里的一类;
- 分类器单独怎么判,**不看这个命令**:``python tools/lora/infer.py --query "..."``
  直接问模型,不花钱在改写上。这里跑的是线上那条完整路径(分类 + 改写);
- 降级原因(``note``)走 **stderr**:它和答案不是一回事,别让它混进管道消费的输出里。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.domain import STRATEGY_NAMES
from project_rag.observability import get_logger

_logger = get_logger("cli.rewrite")

NAME = "rewrite"

_REQUIRED_FACTORY = "query_rewriter_factory"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 rewrite,声明 -q。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="查询改写诊断:判成哪一类、改写成了哪几句(不检索、不生成)"
    )
    parser.add_argument("--question", "-q", required=True, help="要改写的用户问题")
    return parser


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:判定 + 改写 → 打印。输出:退出码 0/1。"""
    from project_rag.config import get_settings

    deps = args.deps
    settings = get_settings()
    if deps.query_rewriter_factory is None:
        print(
            f"缺少依赖 {[_REQUIRED_FACTORY]}:查询改写没装配。"
            "把 config.yaml 的 rewrite.enabled 打开(build_real_deps 才会接上它),"
            "或先跑 tools/lora/train.py 产出适配器",
            file=sys.stderr,
        )
        return 1

    try:
        plan = deps.query_rewriter_factory().rewrite(args.question)
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("查询改写失败:%s", exc)
        _logger.debug("查询改写失败详情:", exc_info=True)
        print(f"查询改写失败:{exc}", file=sys.stderr)
        return 1

    print(f"策略：{plan.strategy}（{STRATEGY_NAMES[plan.strategy]}，标签 {plan.label}）")
    if plan.confidence is None:
        print(f"置信度：-（{plan.decided_by}：没跑分类器）")
    else:
        print(f"置信度：{plan.confidence:.4f}（{plan.decided_by}）")
    print(f"原话：{plan.original}")
    for index, one in enumerate(plan.queries, start=1):
        print(f"检索语句[{index}]：{one}")
    if not plan.changed:
        print("(与原话逐字相同 —— 这一轮不会改变检索行为)")
    if plan.note:
        # 降级原因必须打出来:否则「改写没生效」看起来就和「判成直接检索」一模一样
        print(f"备注：{plan.note}", file=sys.stderr)
    print(
        f"\n当前配置：enabled={settings.rewrite.enabled} "
        f"classifier={settings.rewrite.classifier} device={settings.rewrite.device}"
    )
    return 0
