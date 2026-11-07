"""实盘参数快照（2026-11-07 用户拍板）。

与 :mod:`nanobot_quant.backtest.presets`（内置 preset）的分工：

===========  ==========================  ==============================
            内置 preset                 快照（本模块）
===========  ==========================  ==============================
存放         代码常量 ``presets.py``     持久卷 ``credentials/exec_presets.json``
谁改         只能发版                    用户点按钮（页面「存为快照」）
范围         跨空间一致、可审计          每空间独立
用途         复现研究口径                ①「实盘现在这套跑出来是多少」
                                        ② 改坏了回滚
===========  ==========================  ==============================

快照格式与内置 preset 同构（``created`` / ``label`` / ``run`` / ``overrides``），
因此 :func:`nanobot_quant.backtest.presets.expand_preset` 一份代码能吃两种来源。

**不入快照的字段**（2026-11-07 拍板）：

* ``enabled`` —— 启停是运行时状态；恢复快照不应顺带把 TD 拉起来
* ``execution_channel`` —— 换所是环境变更（台账 / 账户池 / Key 白名单全变）。
  仍记在 ``run.source`` 里作对照，恢复时若与当前不一致必须拒绝
* ``sub_accounts`` —— 账号池属环境配置，默认不覆盖（有 open 持仓时还会触发
  台账重建保护）

``overrides`` 必须是**自包含**的（覆盖 :data:`REQUIRED_OVERRIDE_KEYS` 全集），
解析优先级 = 场景值 → 全局 exec_params → td_params 全局 → ``PARAM_META.std``。
最后一级是「策略类默认值」的代理：``PARAM_META`` 与策略默认值必须保持一致，
两边同时改（有测试锁定 parm meta 的关键默认值）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

__all__ = [
    "EXEC_PRESETS_FILENAME",
    "EXCLUDED_SCENE_KEYS",
    "exec_presets_path",
    "load_user_presets",
    "get_user_preset",
    "save_user_preset",
    "build_snapshot",
    "default_snapshot_name",
    "user_presets_meta",
]

EXEC_PRESETS_FILENAME = "exec_presets.json"

#: 明确排除在快照之外的字段（见模块 docstring）
EXCLUDED_SCENE_KEYS: frozenset[str] = frozenset({"enabled", "execution_channel", "sub_accounts"})

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,60}$")

#: 回测专属键的默认值——它们不在 ``PARAM_META`` / exec_params 里，
#: 取值口径必须与回测侧一致，否则快照会静默换掉闸门状态：
#:
#: * ``ema_gate_enabled`` / ``ema_n`` / ``ema_k`` —— ``backtest/driver.py``
#:   的 ``ema_gate_enabled``(默认 False) / ``EMA_TREND_N``(144) / ``3``
#: * ``tdst_filter`` —— ``td_params`` 默认 False（富途变体恒无效，须保持关）
_BACKTEST_ONLY_DEFAULTS: dict[str, Any] = {
    "ema_gate_enabled": False,
    "ema_n": 144,
    "ema_k": 3,
    "tdst_filter": False,
}


def exec_presets_path() -> Path:
    """快照文件路径：与 ``exec_params.json`` 同目录（持久卷 credentials/）。"""
    from .exec_params import exec_params_path

    return exec_params_path().parent / EXEC_PRESETS_FILENAME


def _write_json(path: Path, data: dict) -> None:
    """原子写（tmp 名带 pid，FUSE 抖动不重试、失败显式抛错）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_user_presets() -> dict[str, dict]:
    """读全部快照；文件缺失 / 损坏 → 空 dict（不抛，避免拖垮页面）。"""
    p = exec_presets_path()
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    presets = data.get("presets") if isinstance(data, dict) else None
    return dict(presets) if isinstance(presets, dict) else {}


def get_user_preset(name: str) -> dict | None:
    return load_user_presets().get(name)


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            "快照名只能包含中文/字母/数字/下划线/连字符，长度 1–60（建议 <主题>_<YYYYMMDD>）"
        )
    return name


def save_user_preset(name: str, snapshot: dict) -> str:
    """写入快照；重名（含内置 preset）直接拒绝，保留历史、不覆盖。

    Raises:
        ValueError: 名字非法、重名、快照不自包含。
    """
    from .backtest.presets import REQUIRED_OVERRIDE_KEYS, builtin_names

    name = _check_name(name)
    if name in builtin_names():
        raise ValueError(f"快照名「{name}」与内置 preset 重名，请换一个名字")
    ov = snapshot.get("overrides") or {}
    missing = sorted(REQUIRED_OVERRIDE_KEYS - set(ov))
    if missing:
        raise ValueError(f"快照不自包含，缺少 {len(missing)} 个决策键：{', '.join(missing[:6])}…")
    all_presets = load_user_presets()
    if name in all_presets:
        raise ValueError(f"快照「{name}」已存在，请换一个名字（快照只追加、不覆盖）")
    all_presets[name] = snapshot
    _write_json(exec_presets_path(), {"presets": all_presets})
    return name


def default_snapshot_name(scene_key: str) -> str:
    return f"live_{scene_key}_{date.today():%Y%m%d}"


def _resolve(key: str, scene: dict, flat: dict, td_defaults: dict) -> Any:
    """场景值 → 全局 exec_params → td_params → PARAM_META.std（类默认值代理）。"""
    from .exec_params import PARAM_META

    for src in (scene, flat, td_defaults):
        v = src.get(key)
        if v is not None:
            return v
    meta = PARAM_META.get(key) or {}
    if "std" in meta:
        return meta["std"]
    if key in _BACKTEST_ONLY_DEFAULTS:
        return _BACKTEST_ONLY_DEFAULTS[key]
    return None


def build_snapshot(scene_key: str = "high") -> dict:
    """把当前实盘某场景解析成自包含快照。

    Raises:
        ValueError: 场景不存在、或某个决策键在四级来源里都取不到值
            （fail-closed：残缺快照比没有快照更危险）。
    """
    from .backtest.presets import REQUIRED_OVERRIDE_KEYS
    from .exec_params import load_exec_params
    from .strategies.registry import load_selected
    from .td_params import load_td_params

    ep = load_exec_params() or {}
    scenes = ep.get("scenes") or {}
    scene = scenes.get(scene_key) or {}
    if not scene:
        raise ValueError(f"场景「{scene_key}」不存在或为空，无法生成快照")

    variant = load_selected()
    td_defaults = load_td_params(variant) or {}
    overrides: dict[str, Any] = {}
    unresolved: list[str] = []
    for key in sorted(REQUIRED_OVERRIDE_KEYS):
        if key in EXCLUDED_SCENE_KEYS:
            continue
        v = _resolve(key, scene, ep, td_defaults)
        if v is None:
            unresolved.append(key)
        else:
            overrides[key] = v
    if unresolved:
        raise ValueError(
            f"以下决策键无法解析出生效值（场景/全局/td_params/默认均缺失）：{', '.join(unresolved)}"
        )

    run = {
        "strategy_variant": variant,
        "scene": scene_key,
        "symbols": list(scene.get("symbols") or ep.get("symbols") or []),
        "periods": [str(scene.get("sleeptime") or ep.get("td_sleeptime") or "1m")],
        "batches": int(scene.get("batches") or ep.get("td_batches") or 1),
        "fixed_amount": float(scene.get("td_fixed_amount") or 0.0) or None,
        "capital_total": float(scene.get("capital_total") or 0.0),
        # 记录了但不参与恢复（见 docstring：换所必须单独走流程）
        "source": str(ep.get("execution_channel") or ""),
    }
    return {
        "created": date.today().isoformat(),
        "label": f"实盘快照 · {scene_key}",
        "description": (
            f"从实盘场景 {scene_key} 冻结（{date.today().isoformat()}）；"
            "用于回测「实盘这套跑出来是多少」与改参数后的回滚"
        ),
        "source": "live_snapshot",
        "run": run,
        "overrides": overrides,
    }


def user_presets_meta() -> list[dict]:
    """快照元信息（不含完整参数），供下拉与列表展示。"""
    out = []
    for name, p in load_user_presets().items():
        out.append(
            {
                "name": name,
                "created": p.get("created"),
                "label": p.get("label", name),
                "description": p.get("description", ""),
                "source": "user",
            }
        )
    return out
