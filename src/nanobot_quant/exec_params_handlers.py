"""Execution parameter handlers — WebUI for exec_params.json.

Registered by gatekeeper as ``/config/exec`` (business management chat,
🛡️ 执行参数 entry).  The page renders the two groups (risk control /
execution quality) as editable cards with per-field bounds; saving
validates everything and persists to ``{data_root}/credentials/exec_params.json``.
Defaults are the pre-parameterisation hardcoded values, so an unmodified
setup behaves exactly as before.

Only the Commander may view/change these parameters — they are the
on-chain risk boundary (position limit, slippage, buffers), so the page
and the save endpoint both enforce ``is_commander``.
"""

from __future__ import annotations

import html
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .exec_params import (
    DEFAULT_SUB_ACCOUNTS,
    GROUP_TITLES,
    PARAM_META,
    SCENES,
    SCENE_FIELD_MAP,
    SCENE_FIELD_ORDER,
    load_exec_params,
    save_exec_params,
)
from .tokens_store import load_token_symbols

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


_PAGE_HTML = _load_template("exec_params_page.html")


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    """Return (error_message_or_None, ok)."""
    _u = request.session.get("user")
    if not _u:
        return "请先登录", False
    if not gatekeeper._platform.is_commander(_u):
        return "仅 Commander 可操作", False
    return None, True


# ── Page rendering ───────────────────────────────────────────────────────

def _channel_family(channel: str) -> str:
    """execution_channel 值 → 执行家族（dex/cex）。

    2026-08-19：/config/exec 按通道过滤参数显示。值域为 broker 实例名
    （okx_dex/gate），兼容旧值 dex/cex 归一化。
    """
    c = str(channel or "")
    if c in ("gate", "cex"):
        return "cex"
    if c in ("okx_dex", "dex"):
        return "dex"
    return "dex"  # 未知值 fail-safe 按默认 DEX 显示


def _showif_attr(key: str, prefix: str = "", fname: str | None = None) -> str:
    """data-show-if 属性（2026-08-20 方案2：④ 仓位与分批互斥显隐）。

    格式 ``data-show-if="quantity_mode=fixed"``；场景字段带前缀 + 场景字段名
    （``scenes_high_quantity_mode=fixed``），前端按完整元素 id 匹配。
    """
    si = PARAM_META[key].get("show_if")
    if not si:
        return ""
    k, v = next(iter(si.items()))
    return f' data-show-if="{prefix}{k}={v}"'


def _field_html(
    key: str,
    value: object,
    options: list[str] | None = None,
    family: str = "dex",
    prefix: str = "",
    field_name: str | None = None,
) -> str:
    meta = PARAM_META[key]
    label = meta.get("label", key)
    hint = meta.get("hint", "")
    std = meta.get("std", "")
    vtype = meta.get("type", "float")
    channels = meta.get("channels", "both")
    # 2026-08-20（S1）：场景字段 id/name 用场景字段名（field_name=场景字段名，
    # 如 enabled/sleeptime/symbols），meta 查表用扁平键（key，如 td_sleeptime）。
    fname = field_name or key
    showif = _showif_attr(key, prefix, fname)
    fid = prefix + fname  # 如 scenes_high_sleeptime（前端 collect() 正则 ^scenes_(high|mid|low)_(.+)$ 解析，场景名固定集合）
    # 2026-08-20：场景字段的通道联动标记（JS applyChannel/applyShowIf 遍历）
    scene_mark = ""
    if fname == "batches":
        scene_mark = ' data-batches-field="1"'
    elif fname == "td_fixed_amount":
        scene_mark = ' data-fam-field="1"'
    # 2026-08-19：批次数量按通道换文案（DEX=子钱包 / CEX=子账号）
    if fname == "batches" and family == "cex":
        label = meta.get("label_cex", label)
        hint = meta.get("hint_cex", hint)
    if vtype == "bool":
        checked = " checked" if value else ""
        return (
            f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label" for="{fid}">{label}</label>'
            f'<label class="switch">'
            f'<input type="checkbox" id="{fid}" name="{fid}"{checked}>'
            f'<span class="slider"></span></label>'
            f'<span class="f-std">默认 {'开' if std else '关'}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "enum":
        choices = meta["enum"]
        labels = meta.get("enum_labels") or {}
        groups = meta.get("enum_groups")

        def _opt(c: str) -> str:
            sel = "selected" if str(value) == c else ""
            return f'<option value="{c}" {sel}>{labels.get(c, c)}</option>'

        if groups:
            grouped = {c for g in groups.values() for c in g}
            parts = []
            for gname, gitems in groups.items():
                parts.append(f'<optgroup label="{gname}">'
                             + "".join(_opt(c) for c in gitems)
                             + "</optgroup>")
            rest = "".join(_opt(c) for c in choices if c not in grouped)
            if rest:
                parts.append(rest)
            opts = "".join(parts)
        else:
            opts = "".join(_opt(c) for c in choices)
        return (
            f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label" for="{fid}">{label}</label>'
            f'<select id="{fid}" name="{fid}">{opts}</select>'
            f'<span class="f-std">默认 {std}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "str":
        choices = options or []
        if choices:
            opts = "".join(
                f'<option value="{c}" {"selected" if str(value) == c else ""}>{c}</option>'
                for c in choices
            )
            if str(value) not in choices:
                opts += f'<option value="{value}" selected>⚙️ 当前: {value}</option>'
            return (
                f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label" for="{fid}">{label}</label>'
                f'<select id="{fid}" name="{fid}">{opts}</select>'
                f'<span class="f-std">默认 {std} · tokens.json 登记代币</span>'
                f'<span class="f-hint">{hint}</span></div>'
            )
        return (
            f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label" for="{fid}">{label}</label>'
            f'<input type="text" id="{fid}" name="{fid}" value="{value}">'
            f'<span class="f-std">默认 {std}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "list":
        # 多选 checkbox（同名 name，collect() 收集为数组）
        choices = options or []
        values = value if isinstance(value, list) else [value]
        # 标的池（td_symbols）：行式编辑——每候选一行，附 保留量(min_hold)
        # / 成本价(cost_price) 输入（写入 tokens.json，见 save 的 meta 处理）。
        if key == "td_symbols":
            meta = _token_meta_map(choices, values)
            # 优先级=池子顺序：勾选行按当前保存顺序渲染（上下移生效后可见），
            # 未勾选行按候选顺序附加。
            ordered = [c for c in values if c in choices] + [
                c for c in choices if c not in values]
            rows = []
            for c in ordered:
                m = meta.get(c, {})
                # 2026-08-19：保留量(min_hold)仅 DEX 显示——span 带
                # data-channel="dex"，前端 JS 按执行通道隐藏/显示；
                # CEX 下不提交（tokens.json 旧值保留，切回 DEX 恢复）。
                hold_cell = (
                    f'<span class="pool-meta" data-channel="dex">保留量 '
                    f'<input type="number" name="meta_min_hold" data-sym="{c}" '
                    f'value="{m.get("min_hold", 0.0)}" min="0" step="0.001"></span>'
                )
                rows.append(
                    f'<div class="pool-row">'
                    f'<button class="pool-mv" type="button" '
                    f'onclick="movePoolRow(this.closest(\'.pool-row\'), -1)" '
                    f'title="上移（提高优先级）">↑</button>'
                    f'<button class="pool-mv" type="button" '
                    f'onclick="movePoolRow(this.closest(\'.pool-row\'), 1)" '
                    f'title="下移（降低优先级）">↓</button>'
                    f'<label class="chk"><input type="checkbox" class="multi" '
                    f'name="{fid}" value="{c}"'
                    f'{" checked" if c in values else ""}>{c}</label>'
                    f'{hold_cell}'
                    f'<span class="pool-meta">成本价 '
                    f'<input type="number" name="meta_cost_price" data-sym="{c}" '
                    f'value="{m.get("cost_price", "") or ""}" min="0" '
                    f'step="0.0001" placeholder="对账价兜底"></span>'
                    f'</div>'
                )
            # 当前值里不在候选列表的（如旧值）也显示，避免保存时被静默丢弃
            for v in values:
                if v not in choices:
                    rows.append(
                        f'<div class="pool-row"><label class="chk">'
                        f'<input type="checkbox" class="multi" name="{fid}" '
                        f'value="{v}" checked>⚙️ {v}</label></div>'
                    )
            return (
                f'<div class="field" data-channel="both"{showif}{scene_mark}><label class="f-label">{label}</label>'
                f'<div class="pool">{"".join(rows)}</div>'
                f'<span class="f-std pool-fstd" data-fstd-dex="默认 {std} · tokens.json 登记代币（多选；↑↓ 调整优先级——同 bar 多标的 Setup 9 按此顺序依次执行；保留量=每账户最低持有，成本价=天然持仓导入价）" data-fstd-cex="默认 {std} · tokens.json 登记代币（多选；↑↓ 调整优先级——同 bar 多标的 Setup 9 按此顺序依次执行；成本价=天然持仓导入价）">默认 {std} · tokens.json 登记代币（多选；↑↓ 调整优先级——同 bar 多标的 Setup 9 按此顺序依次执行；{"保留量=每账户最低持有，" if family == "dex" else ""}成本价=天然持仓导入价）</span>'
                f'<span class="f-hint">{hint}</span></div>'
            )
        boxes = [
            f'<label class="chk"><input type="checkbox" class="multi" name="{fid}" '
            f'value="{c}"{" checked" if c in values else ""}>{c}</label>'
            for c in choices
        ]
        # 当前值里不在候选列表的（如旧值）也显示，避免保存时被静默丢弃
        for v in values:
            if v not in choices:
                boxes.append(
                    f'<label class="chk"><input type="checkbox" class="multi" '
                    f'name="{fid}" value="{v}" checked>⚙️ {v}</label>'
                )
        return (
            f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label">{label}</label>'
            f'<div class="chk-group">{"".join(boxes)}</div>'
            f'<span class="f-std">默认 {std} · {"可多选" if key == "sub_accounts" else "tokens.json 登记代币（多选）"}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    lo, hi = meta["min"], meta["max"]
    step = str(meta.get("step", 0.01))
    return (
        f'<div class="field" data-channel="{channels}"{showif}{scene_mark}><label class="f-label" for="{fid}">{label}</label>'
        f'<input type="number" id="{fid}" name="{fid}" value="{value}" '
        f'min="{lo}" max="{hi}" step="{step}">'
        f'<span class="f-std">默认 {std} · 范围 {lo}–{hi}</span>'
        f'<span class="f-hint">{hint}</span></div>'
    )


def _scene_card_html(
    scene_key: str,
    scene: dict,
    options: dict[str, list[str]] | None,
    family: str,
    gate_accounts: list[str],
    idx: int,
) -> str:
    """④ 多场景卡片（S1 配置层，2026-08-20）。

    每场景独立：启用/周期/标的池/数量/批次/子账号池/出场参数。
    high 场景与扁平参数同步（S1 阶段 td_live 消费扁平）。
    字段 id/name 用场景字段名（field_name=fk），meta 查表用扁平键。
    """
    sdef = SCENES[scene_key]
    prefix = f"scenes_{scene_key}_"
    opts = options or {}
    fields = []
    for fk in SCENE_FIELD_ORDER:
        pk = SCENE_FIELD_MAP.get(fk, fk)  # sub_accounts 无扁平键 → 自身
        if fk == "sub_accounts":
            fields.append(_field_html(pk, scene.get(fk), gate_accounts, family,
                                      prefix, field_name=fk))
        else:
            fields.append(_field_html(pk, scene.get(fk), opts.get(pk), family,
                                      prefix, field_name=fk))
    badge = "🟢 启用" if scene.get("enabled") else "⚪ 停用"
    return (
        f'<div class="card scene-card" data-scene="{scene_key}">'
        f'<h3>④-{idx} {sdef["label"]}（{sdef["freq"]} · {sdef["desc"]}）　{badge}</h3>'
        f'{"".join(fields)}'
        f'</div>'
    )


def _group_html(
    group: str, params: dict, options: dict[str, list[str]] | None = None, family: str = "dex"
) -> str:
    # 2026-08-19：所有字段渲染（含 data-channel 属性），前端 JS 按当前
    # 执行通道统一显示/隐藏——服务端不过滤，切换通道往返不丢 DOM。
    fields = "".join(
        _field_html(k, params[k], (options or {}).get(k), family)
        for k in PARAM_META
        if PARAM_META[k].get("group") == group
    )
    if not fields:
        return ""
    return f'<div class="card"><h3>{GROUP_TITLES[group]}</h3>{fields}</div>'


def _strategy_banner_html() -> str:
    """TD 循环当前策略模块横幅（2026-08-19）。

    区分 strategy.json 目标值与运行中循环实际值：切换策略后不重启
    TD 循环（td_enabled 关→开）时两者不一致，横幅显式提示。
    """
    try:
        from .strategies.registry import get_strategy, load_selected
        from .td_live_state import get_state as live_state

        name = load_selected() or ""
        try:
            label = get_strategy(name).label
        except Exception:  # noqa: BLE001
            label = name
        st = live_state()
        running = bool(st.get("running"))
        cur = st.get("strategy_variant") or ""
        link = '<a href="/config/strategy" style="color:#4527a0;font-weight:600">⇄ 策略选择</a>'
        base = f"🧭 当前策略模块：<b>{html.escape(label)}</b>（{html.escape(name)}）"
        if running and cur and cur != name:
            try:
                cur_label = get_strategy(cur).label
            except Exception:  # noqa: BLE001
                cur_label = cur
            return (
                f'<div class="banner strategy">{base}'
                f'｜ ⚙️ 运行中：<b>{html.escape(cur_label)}</b>'
                f'（切换未生效——重启 TD 循环后应用）　{link}</div>'
            )
        if running:
            return (
                f'<div class="banner strategy">{base}'
                f'｜ ⚙️ 运行中：{html.escape(label)}　{link}</div>'
            )
        return f'<div class="banner strategy">{base}｜ ⏸️ TD 循环未运行　{link}</div>'
    except Exception:  # noqa: BLE001
        return ""


def _gate_accounts() -> list[str]:
    """Gate 子账号可选列表（gate.json slot_map / sub_accounts），兜底 gate_bot1-5。

    S1 配置层：子账号池多选来源；S3 消费时按 gate.json 实际账号验证。
    """
    try:
        from .gate_credentials import load_gate_credentials, load_slot_map

        creds = load_gate_credentials() or {}
        slot_map = load_slot_map(creds)
        names: list[str] = []
        for i in sorted(slot_map, key=int):
            if slot_map[i] not in names:
                names.append(slot_map[i])
        for k in (creds.get("sub_accounts") or {}).keys():
            if str(k) not in names:
                names.append(str(k))
        if names:
            return names
    except Exception:
        pass
    return list(DEFAULT_SUB_ACCOUNTS)


def _render_page(params: dict, message: str = "") -> str:
    try:
        from .exec_params import exec_params_path
        custom = exec_params_path().is_file()
    except Exception:
        custom = False
    banner = (
        '<div class="banner custom">⚙️ 已自定义执行参数（exec_params.json）——'
        '如需恢复默认，点击「恢复默认」或删除该文件</div>'
        if custom
        else '<div class="banner default">默认参数（= 旧版硬编码行为，零变化）</div>'
    )
    banner += (
        '<div class="banner locked">🔒 系统级风控参数：仅 Commander 可修改，'
        'MCP/LLM 不可传（调用级 portfolio_value / quantity 除外）</div>'
    )
    msg = (
        f'<div class="banner msg" id="msg">{message}</div>'
        if message
        else '<div class="banner msg hidden" id="msg"></div>'
    )
    # TD target options = managed tokens.json entries, minus stablecoins
    # (no analysis value). Native coin SOL appears here once registered
    # via /config/tokens (address auto-filled from builtin whitelist).
    _STABLECOINS = {"USDC", "USDT"}
    token_opts = {"td_symbols": [s for s in load_token_symbols()
                                  if s not in _STABLECOINS]}
    family = _channel_family(params.get("execution_channel", "okx_dex"))
    gate_accounts = _gate_accounts()
    groups = "".join(
        _group_html(g, params, token_opts, family)
        for g in ("risk", "exec", "td")
    )
    scenes = params.get("scenes") or {}
    scene_cards = "".join(
        _scene_card_html(sk, scenes.get(sk, {}), token_opts, family,
                         gate_accounts, i + 1)
        for i, sk in enumerate(SCENES)
    )
    return (
        _PAGE_HTML.replace("{banner}", banner)
        .replace("{msg}", msg)
        .replace("{td_strategy_banner}", _strategy_banner_html())
        .replace("{groups}", groups)
        .replace("{scene_cards}", scene_cards)
    )


def _token_meta_map(
    choices: list[str], values: list[str]
) -> dict[str, dict[str, Any]]:
    """标的池行式编辑所需元数据：{symbol: {min_hold, cost_price}}。

    数据源 = tokens.json（token_meta）；缺失/未登记条目返回空 dict。
    """
    try:
        from .tokens_store import token_meta
    except Exception:
        return {}
    meta: dict[str, dict[str, Any]] = {}
    for c in choices:
        meta[c] = {"min_hold": token_meta(c).get("min_hold", 0.0),
                   "cost_price": token_meta(c).get("cost_price")}
    return meta


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Handlers ─────────────────────────────────────────────────────────────

async def exec_params_page(request: Request) -> HTMLResponse:
    """GET /config/exec — editable parameter cards (Commander only)."""
    _u = request.session.get("user")
    if not _u:
        return HTMLResponse(
            "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 请先登录</h3>",
            status_code=401,
        )
    params = load_exec_params()
    return HTMLResponse(_render_page(params))


async def exec_params_save(request: Request) -> JSONResponse:
    """POST /config/exec — validate + persist (Commander only)."""
    data = await _body(request)
    if data is None:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    result = save_exec_params(data)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error", "保存失败")},
                            status_code=400)
    _persist_token_meta(data)
    return JSONResponse({"ok": True, "message": "执行参数已保存并即时生效",
                         "params": result.get("params")})


def _persist_token_meta(data: dict) -> None:
    """把标的池行式编辑的 min_hold / cost_price 写回 tokens.json。

    body 中 meta_min_hold / meta_cost_price 为 {symbol: value} 映射
    （前端 collect() 收集），仅写有值的标的，失败静默（主保存已成功）。
    """
    try:
        from .tokens_store import update_token_meta

        holds = data.get("meta_min_hold") or {}
        costs = data.get("meta_cost_price") or {}
        for sym in set(list(holds.keys()) + list(costs.keys())):
            update_token_meta(
                sym,
                min_hold=holds.get(sym),
                cost_price=costs.get(sym),
            )
    except Exception:
        pass


# ── Route registration helper ───────────────────────────────────────────

def register_exec_params_routes(app, gatekeeper) -> None:
    """Register execution parameter routes on the FastAPI app.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """

    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(
                f"<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 {err}</h3>",
                status_code=403 if "Commander" in err else 401,
            )
        params = load_exec_params()
        return HTMLResponse(_render_page(params))

    async def _save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err},
                                status_code=403 if "Commander" in err else 401)
        data = await _body(request)
        if data is None:
            return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
        result = save_exec_params(data)
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "保存失败")},
                                status_code=400)
        _persist_token_meta(data)
        # 批次（子钱包/子账号）初始化：按场景建/复用独立台账
        #（S3b 2026-08-20：batches.{channel}.{scene}.{symbol}.json，
        # 同标的跨场景互不复用）；无 scenes 时回退旧扁平 td_* 路径。
        try:
            from nanobot_quant.batches import ensure_batches

            _channel = result["params"].get("execution_channel", "okx_dex")
            _scenes = result["params"].get("scenes") or {}
            _msgs = []
            for _name, _sc in _scenes.items():
                if not _sc.get("enabled") or int(_sc.get("batches", 1) or 1) <= 1:
                    continue
                _syms = _sc.get("symbols") or []
                for _sym in _syms:
                    _b, _msg = ensure_batches(
                        int(_sc.get("batches", 1) or 1),
                        _sym,
                        _channel,
                        scene=_name,
                    )
                    _msgs.append(f"{_sym}[{_name}]: {_msg}")
            if not _scenes:
                _symbols = result["params"].get("td_symbols") or ["SOL"]
                for _sym in _symbols:
                    _b, _msg = ensure_batches(
                        int(result["params"].get("td_batches", 1) or 1),
                        _sym,
                        _channel,
                    )
                    _msgs.append(f"{_sym}: {_msg}")
            gatekeeper._log(f"🧩 批次同步: {'; '.join(_msgs)}")
        except Exception as exc:
            gatekeeper._log(f"⚠️ 批次同步失败: {exc}")
        # TD 自主循环按新参数同步启停（td_enabled 开关 + 参数热更新）
        try:
            from nanobot_quant.td_live import sync_from_params

            td_status = sync_from_params(result["params"])
            gatekeeper._log(
                f"🔄 TD live 同步: running={td_status.get('running')} "
                f"thread_alive={td_status.get('thread_alive')}"
            )
        except Exception as exc:
            gatekeeper._log(f"⚠️ TD live 同步失败: {exc}")
        gatekeeper._log(
            f"🛡️ 执行参数已更新: "
            f"position={result['params'].get('max_position_pct')} "
            f"drawdown={result['params'].get('max_drawdown_pct')} "
            f"stop_loss={result['params'].get('stop_loss_pct')} "
            f"slippage={result['params'].get('slippage')} "
            f"sol_buffer={result['params'].get('sol_buffer_pct')} "
            f"td_enabled={result['params'].get('td_enabled')} "
            f"td_symbols={result['params'].get('td_symbols')} "
            f"td_sleeptime={result['params'].get('td_sleeptime')} "
            f"quantity_mode={result['params'].get('quantity_mode')} "
            f"td_quantity={result['params'].get('td_quantity')} "
            f"td_batches={result['params'].get('td_batches')} "
            f"exit_order={result['params'].get('exit_order')} "
            f"take_profit={result['params'].get('take_profit_pct')}"
        )
        return JSONResponse({"ok": True, "message": "执行参数已保存并即时生效",
                             "params": result.get("params")})

    app.add_route("/config/exec", _page, methods=["GET"])
    app.add_route("/config/exec", _save, methods=["POST"])
