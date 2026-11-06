"""入场多周期（entry_periods）回测覆盖字段的结构性测试（Step 3 批 2）。

锁三件事：
  1) ``backtest_handlers._OV_FIELDS`` 注册了场景级 ``entry_periods``（默认 "1"）
  2) ``coerce_overrides`` 对它做校验 + 规范化，非法值 fail-closed
  3) 页面三处齐全：控件、JS 收参、参数表渲染行

为什么第 2 条必须有：策略侧 ``parse_entry_periods`` 遇非法项是**静默忽略并
回退 [1]**（保证实盘加载旧配置不崩）。若入口不拦，页面填 ``1,7`` 会变成
「以为开了多周期、实际只跑单周期」——回测结论直接失真，且事后无法分辨。
"""

from pathlib import Path

import pytest

from nanobot_quant import backtest_handlers as bh

_PAGE = Path(bh.__file__).with_name("backtest_page.html")


def test_ov_fields_has_entry_periods_scene_default():
    assert bh._OV_FIELDS["entry_periods"] == ("scene", None, "1")


def test_coerce_entry_periods_normalizes():
    assert bh.coerce_overrides({"entry_periods": "1"}) == {"entry_periods": "1"}
    assert bh.coerce_overrides({"entry_periods": "1,2,3,4,5,8"}) == {
        "entry_periods": "1,2,3,4,5,8"
    }
    # 全角逗号 / 多余空格 / 乱序 / 重复 → 规范化成升序去重
    assert bh.coerce_overrides({"entry_periods": " 8，2 , 1 "}) == {
        "entry_periods": "1,2,8"
    }
    assert bh.coerce_overrides({"entry_periods": "2,1,2"}) == {"entry_periods": "1,2"}


@pytest.mark.parametrize("bad", ["1,7", "9", "1,x", "0", "-1", " ", "1.5", "8,99"])
def test_coerce_entry_periods_invalid_fail_closed(bad):
    with pytest.raises(ValueError):
        bh.coerce_overrides({"entry_periods": bad})


def test_page_has_entry_periods_wiring():
    html = _PAGE.read_text(encoding="utf-8")
    assert 'id="entry_periods"' in html  # 控件
    assert '"entry_periods"' in html  # OV_KEYS + collectOverrides
    assert "入场多周期" in html  # 参数表标签
    assert "关（只吃基础周期）" in html  # 单周期时的显示文案
