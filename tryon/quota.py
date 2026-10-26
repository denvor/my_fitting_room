"""额度：调本机的 qianwen 命令查 token plan，按 1K 一档折算还能用多少次。"""

import json
import shutil
import subprocess
from dataclasses import dataclass

CREDITS_PER_1K = 58  # 实测：一次成功的 1K 生成扣 58 credit
CLI_TIMEOUT = 20


@dataclass(frozen=True)
class Quota:
    total: float
    used: float
    times_1k: int
    remaining_days: int


class QuotaUnavailable(Exception):
    """额度查不到，消息是给人看的中文。"""


def _run(argv: list[str]) -> str:
    """唯一联网的一段，测试里换掉。"""
    return subprocess.run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT).stdout


def read_quota(run=_run) -> Quota:
    path = shutil.which("qianwen")
    if not path:
        raise QuotaUnavailable("本机没有 qianwen 命令，查不到额度")
    try:
        out = run([path, "subscription", "status"])
    except Exception as e:
        raise QuotaUnavailable(f"额度查询失败：{e}")
    try:
        individual = json.loads(out)["individual"]
        credits = individual["monthlyCredits"]
        total, used_pct = float(credits["total"]), float(credits["usedPct"])
    except Exception as e:
        raise QuotaUnavailable(f"读不懂 qianwen 的输出：{e}")
    used = total * used_pct / 100
    return Quota(
        total=total,
        used=used,
        times_1k=max(0, int((total - used) // CREDITS_PER_1K)),
        remaining_days=int(individual.get("remainingDays") or 0),
    )
