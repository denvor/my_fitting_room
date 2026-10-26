import json

import pytest

from tryon import quota as quota_mod
from tryon.quota import QuotaUnavailable, read_quota

# 真实输出的一小段，字段名和数值都照 qianwen subscription status 抄
SAMPLE = json.dumps(
    {
        "individual": {
            "remainingDays": 13,
            "monthlyCredits": {"total": 25500, "usedPct": 42.18340028235295},
        }
    }
)


def fake_run(payload):
    """替身：假装是 qianwen 命令，记下参数并吐出给定文本。"""

    def run(argv):
        run.argv = argv
        return payload

    return run


@pytest.fixture(autouse=True)
def cli_exists(monkeypatch):
    """测试不依赖本机真装了 qianwen。"""
    monkeypatch.setattr(quota_mod.shutil, "which", lambda name: "/usr/local/bin/qianwen")


def test_read_quota_folds_credits_into_1k_count():
    run = fake_run(SAMPLE)
    q = read_quota(run=run)

    assert run.argv[0].endswith("qianwen") and run.argv[1:] == ["subscription", "status"]
    assert q.total == 25500
    assert q.used == pytest.approx(10756.767, abs=0.001)
    assert q.times_1k == 254  # (25500 - 10756.767) // 58
    assert q.remaining_days == 13


def test_read_quota_never_goes_negative():
    """额度用超或读数抖动时，次数不能变成负数。"""
    run = fake_run(json.dumps({"individual": {"monthlyCredits": {"total": 100, "usedPct": 100}}}))
    assert read_quota(run=run).times_1k == 0


def test_missing_cli_reports_readable_error(monkeypatch):
    monkeypatch.setattr(quota_mod.shutil, "which", lambda name: None)
    with pytest.raises(QuotaUnavailable) as e:
        read_quota(run=fake_run(SAMPLE))
    assert "qianwen" in str(e.value)


def test_unparseable_output_reports_readable_error():
    with pytest.raises(QuotaUnavailable) as e:
        read_quota(run=fake_run("<html>登录失效</html>"))
    assert "读不懂" in str(e.value)


def test_cli_failure_is_wrapped():
    def explode(argv):
        raise OSError("命令超时")

    with pytest.raises(QuotaUnavailable) as e:
        read_quota(run=explode)
    assert "命令超时" in str(e.value)
