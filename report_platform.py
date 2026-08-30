# -*- coding: utf-8 -*-
"""
财报分析平台 (Streamlit)
========================
搜索一只 A 股 → 总览 / 三张报表 / 券商研报 交互查看。

运行:
    streamlit run report_platform.py

数据层复用同目录 quick_report.py（取数逻辑一致，API Key 配置也复用 hithink_config.json）。
Streamlit 对取数做了缓存（避开同花顺调用配额）。

对比口径（重要）:
  - 利润表 / 现金流量表是【累计口径】: 一季=3个月、中报=半年、三季=9个月、年报=1年，
    长度不同直接并列会失真 → 平台默认用「单季」(每列都是3个月，可比)，
    也可切「中报 H1」/「年报 FY」看同口径同比。
  - 资产负债表是【时点快照】(期末余额)，不同报告期可直接并列，默认「全部期末」。
  - 比率类指标(毛利率/净利率/费用率)是百分比，跨尺度可比，趋势图不受影响。
"""

import re
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.graph_objects as go

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 复用 quick_report 数据层
sys.path.insert(0, str(Path(__file__).resolve().parent))
import quick_report as qr  # noqa: E402

st.set_page_config(page_title='财报分析平台', page_icon='📊', layout='wide')

APP_TITLE = '📊 财报分析平台'
APP_HINT = '搜索股票 → 总览 / 三表 / 研报。数据：同花顺(财报) + 东财(研报)，仅供参考，非投资建议。'

# ========== 中英双语辅助 ==========
_CN2EN = {cn: en for cn, en in qr.INDICATOR_LABELS.values()}


def bi(cn, en):
    """科目中文 + 英文"""
    return f'{cn} {en}' if en else cn


def _qlabel(y, fp):
    """单季列标签: 2026Q2 / 2026Q1 ..."""
    return f'{y}Q{qr.FP_ORDER.get(fp, "?")}'


# ========== 缓存取数（TTL 内不重复调接口） ==========
@st.cache_data(ttl=3600, show_spinner=False)
def cached_resolve(q):
    return qr.resolve_stock(q)


@st.cache_data(ttl=600, show_spinner=False)
def cached_snapshot(ths):
    return qr.get_snapshot(ths)


@st.cache_data(ttl=600, show_spinner=False)
def cached_valuations(ths):
    return qr.get_valuations(ths)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_statement(ths, st_key, limit):
    meta = qr.STATEMENTS[st_key]
    return qr.ths_get(meta['path'], {'thscode': ths, 'period': 'quarterly', 'limit': limit})


@st.cache_data(ttl=3600, show_spinner=False)
def cached_indicators(ths):
    return qr.get_indicators_latest(ths)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_reports(ticker):
    return qr.get_reports_eastmoney(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_report_detail(info_code):
    return qr.get_report_detail(info_code)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_concept_boards(ticker):
    return qr.get_concept_boards(ticker)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_board_stocks(board_code):
    return qr.get_board_stocks(board_code)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_concept_graph(stock_label, boards_tuple, max_stocks):
    return qr.build_concept_graph_html(stock_label, list(boards_tuple), max_stocks)


@st.cache_data(ttl=3600, show_spinner='正在取成分股财务数据(市值前12, 同花顺)...')
def cached_board_financials(board_code, ticker):
    """板块成分股财务表: 市值/PE/PB(东财, 全量) + 毛利率/营收增速/净利增速/ROE(同花顺, 市值前12)"""
    stocks = qr.get_board_stocks(board_code, limit=200)
    if not stocks:
        return None
    top = sorted(stocks, key=lambda s: -(s.get('mcap') or 0))[:12]
    rows = []
    for s in top:
        ths = qr._guess_thscode(s['code'])
        fin = qr.get_financial_core(ths) if ths else None
        rows.append({
            '名称': s['name'],
            '总市值(亿)': round(s['mcap'] / 1e8, 1) if s.get('mcap') else None,
            'PE(动)': round(s['pe'], 1) if s.get('pe') else None,
            'PB': round(s['pb'], 1) if s.get('pb') else None,
            '毛利率%': (fin or {}).get('gross_margin'),
            '营收增速%': (fin or {}).get('rev_yoy'),
            '净利增速%': (fin or {}).get('np_yoy'),
            'ROE%': (fin or {}).get('roe'),
        })
    return pd.DataFrame(rows) if rows else None


# ========== 估值判断(Tab8) 缓存 ==========
@st.cache_data(ttl=3600, show_spinner=False)
def cached_main_business(ths):
    return qr.get_main_business(ths)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_board_valuation(board_code):
    return qr.board_valuation_stats(board_code)


@st.cache_data(ttl=3600, show_spinner='正在计算主营相似度(逐家取主营构成, 首次稍慢)...')
def cached_similarity(ths, peers_tuple):
    return qr.main_business_similarity(ths, [{'code': c, 'name': n} for c, n in peers_tuple])


@st.cache_data(ttl=3600, show_spinner='正在取历史估值(腾讯K线×EPS近似)...')
def cached_val_history(ths):
    return qr.get_valuation_history(ths, years=5)


REPORTS_DIR = Path(qr.cfg_get('reports_dir') or
                     (Path(__file__).resolve().parent / 'reports' / '研报'))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _report_pdf_path(title):
    safe = re.sub(r'[\\/:*?"<>|]', '', title or '')[:80] or 'report'
    return REPORTS_DIR / f'{safe}.pdf'


def _safe_stem(title):
    return re.sub(r'[\\/:*?"<>|]', '', title or '')[:80] or 'report'


@st.dialog('⬇️ 下载研报 PDF · 指定保存位置')
def download_report_dialog(title, pdf_url):
    """弹窗: 输入保存文件夹(记住上次位置), 确认后下载"""
    default_dir = str(st.session_state.get('last_dl_dir') or REPORTS_DIR)
    st.caption(f'**{title}**')
    d = st.text_input('保存文件夹（绝对路径，如 D:/研报下载）', value=default_dir)
    if st.button('✅ 确认下载', type='primary'):
        d = (d or '').strip()
        if not d:
            st.error('请输入保存目录')
        else:
            try:
                p = Path(d) / f'{_safe_stem(title)}.pdf'
                if qr.download_report_pdf(pdf_url, p):
                    st.session_state['last_dl_dir'] = str(Path(d))
                    st.success(f'已保存：`{p}`')
                else:
                    st.error('下载失败（反爬校验未通过）')
            except Exception as e:
                st.error(f'下载失败：{e}')
    if st.button('取消'):
        st.rerun()


# ========== 数据组装（同口径） ==========
SCOPE_NAMES = {'single': '单季 QoQ (3个月)', 'h1': '中报 H1 同口径',
               'fy': '年报 FY 同口径', 'all': '期末快照 All (时点)'}


def statement_df(ths, st_key, limit, scope='single'):
    """三表明细 → (DataFrame, series, err)

    scope: single(单季, 每列3个月) / h1(仅中报) / fy(仅年报) / all(期末快照)
    series: {科目label: {'kind', 'periods', 'values'}} 供点击科目画折线图
    """
    meta = qr.STATEMENTS[st_key]
    j = cached_statement(ths, st_key, limit)
    if not j or j.get('code') != 0:
        return pd.DataFrame(), {}, (j or {}).get('message') or '请求失败'
    items = (j.get('data') or {}).get('item', [])
    if not items:
        return pd.DataFrame(), {}, '无数据'
    is_flow = meta['kind'] == 'flow'
    cmap = {(it.get('fiscal_year'), it.get('fiscal_period')): it for it in items
            if it.get('fiscal_year') is not None and it.get('fiscal_period')}
    single = qr.build_single_quarter(items) if is_flow else {}

    # --- 选列：同口径，时间轴统一 旧→新 (左→右) ---
    if is_flow and scope == 'single':
        keys, col_items, col_labels = qr.single_quarter_series(items)
    else:
        target = {'h1': 2, 'fy': 4}.get(scope) if scope in ('h1', 'fy') else None
        keys = sorted((k for k in cmap if target is None or qr.FP_ORDER.get(k[1]) == target),
                      reverse=True)
        col_items = [cmap[k] for k in keys]
        col_labels = [qr._period_label(y, fp) for (y, fp) in keys]
    keys = keys[::-1]
    col_items = col_items[::-1]
    col_labels = col_labels[::-1]
    if not col_items:
        return pd.DataFrame(), {}, '该口径暂无数据'
    cur_key = keys[-1]          # 最新在最右
    cur = col_items[-1]

    def _yoy_item():
        if is_flow and scope == 'single':
            return single.get((cur_key[0] - 1, cur_key[1]))
        return cmap.get((cur_key[0] - 1, cur_key[1]))

    def _prev_item():
        # 环比: 单季看前一单季(最新左边一列); 期末快照看上一期末; 同口径(h1/fy)无环比
        if (is_flow and scope == 'single') or ((not is_flow) and scope == 'all'):
            return col_items[-2] if len(col_items) > 1 else None
        return None

    def _disp(v, kind):
        if v is None:
            return '-'
        return qr.fmt_yi(v) if kind == 'amount' else f'{v:.2f}'

    rows, series = [], {}
    for fname, label, en, kind in meta['fields']:
        key = bi(label, en)
        row, vals = {'科目': key}, []
        for lbl, it in zip(col_labels, col_items):
            v = qr._field_value(fname, it)
            row[lbl] = _disp(v, kind)
            vals.append(v)
        row['同比'] = qr._fmt_cmp(qr._field_value(fname, cur),
                                  qr._field_value(fname, _yoy_item()), kind)
        pv = _prev_item()
        row['环比'] = (qr._fmt_cmp(qr._field_value(fname, cur),
                                   qr._field_value(fname, pv), kind) if pv else '-')
        rows.append(row)
        series[key] = {'kind': kind, 'periods': list(col_labels), 'values': vals}
    return pd.DataFrame(rows), series, None


def statement_chart(ths, st_key, limit, cols, scope='single'):
    """三表关键科目 同口径柱状图（金额已转亿）"""
    meta = qr.STATEMENTS[st_key]
    j = cached_statement(ths, st_key, limit)
    items = ((j or {}).get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
    if not items:
        return None
    is_flow = meta['kind'] == 'flow'
    if is_flow and scope == 'single':
        _k, seq, labels = qr.single_quarter_series(items)
    else:
        target = {'h1': 2, 'fy': 4}.get(scope) if scope in ('h1', 'fy') else None
        seq = [it for it in items if target is None or qr.FP_ORDER.get(it.get('fiscal_period')) == target]
        seq = seq[:limit]
        labels = [qr._period_label(it.get('fiscal_year'), it.get('fiscal_period')) for it in seq]
    seq = seq[::-1]       # 时间轴: 旧→新 (左→右)
    labels = labels[::-1]
    data = []
    for it, lbl in zip(seq, labels):
        pt = {'报告期': lbl}
        for key, name in cols:
            v = it.get(key)
            pt[name] = round(v / 1e8, 1) if isinstance(v, (int, float)) else 0.0
        data.append(pt)
    return pd.DataFrame(data)


def income_ratio_df(ths, limit):
    """利润表比率趋势: 毛利率/净利率/费用率(%)（比率跨尺度可比）"""
    j = cached_statement(ths, 'income', limit)
    items = ((j or {}).get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
    rows = []
    for it in items[:limit]:
        op = it.get('operating_income')
        if not op:
            continue
        lbl = qr._period_label(it.get('fiscal_year'), it.get('fiscal_period'))
        row = {'报告期': lbl,
               '毛利率': round(qr._div_pct(qr._sub(op, it.get('operating_costs')), op), 2),
               '净利率': round(qr._div_pct(it.get('net_profit'), op), 2)}
        for key, name in [('sales_fee', '销售费用率'), ('manage_fee', '管理费用率'),
                          ('research_and_development_expenses', '研发费用率')]:
            row[name] = round(qr._div_pct(it.get(key), op), 2)
        rows.append(row)
    return pd.DataFrame(rows[::-1]) if rows else None


def single_quarter_df(ths, limit):
    """单季营收/归母净利（由累计期推导，看环比动能）"""
    j = cached_statement(ths, 'income', limit)
    items = ((j or {}).get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
    if not items:
        return None
    _keys, col_items, col_labels = qr.single_quarter_series(items)
    rows = []
    for k, s, lbl in zip(reversed(_keys), reversed(col_items), reversed(col_labels)):
        rows.append({'报告期': lbl,
                     '单季营收': round((s.get('operating_income') or 0) / 1e8, 1),
                     '单季归母净利': round((s.get('parent_holder_net_profit') or 0) / 1e8, 1)})
    return pd.DataFrame(rows)


def h1_income_df(ths, limit):
    """中报同口径: 仅历年 H1（营收/归母净利，升序）"""
    j = cached_statement(ths, 'income', limit)
    items = ((j or {}).get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
    rows = []
    for it in items:
        if qr.FP_ORDER.get(it.get('fiscal_period')) == 2:  # 中报
            rows.append({'报告期': qr._period_label(it.get('fiscal_year'), it.get('fiscal_period')),
                         '营业收入': round((it.get('operating_income') or 0) / 1e8, 1),
                         '归母净利润': round((it.get('parent_holder_net_profit') or 0) / 1e8, 1)})
    return pd.DataFrame(rows[::-1]) if rows else None


# ========== 图表渲染 ==========
def render_bar_chart(df, ycols, title, unit='亿元'):
    fig = go.Figure()
    for c in ycols:
        fig.add_bar(x=df['报告期'], y=df[c], name=c)
    fig.update_layout(title=title, barmode='group',
                      xaxis_title='', yaxis_title=unit, height=340,
                      margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, width='stretch')


def render_line_chart(df, ycols, title, unit='%'):
    fig = go.Figure()
    for c in ycols:
        fig.add_scatter(x=df['报告期'], y=df[c], mode='lines+markers', name=c,
                        connectgaps=True)
    fig.update_layout(title=title, xaxis_title='', yaxis_title=unit, height=320,
                      margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, width='stretch')


def render_radar(scores):
    """因子强度雷达图(0-100)"""
    dims = list(scores.keys())
    if not dims:
        return
    vals = [float(scores[d]) for d in dims] + [float(scores[dims[0]])]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=dims + [dims[0]], fill='toself',
                                    line=dict(color='#457b9d'),
                                    marker=dict(color='#457b9d')))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
                      showlegend=False, height=400,
                      title='因子强度雷达 Factor Radar（0-100）',
                      margin=dict(l=40, r=40, t=60, b=20))
    st.plotly_chart(fig, width='stretch')


# ========== 侧边栏 ==========
with st.sidebar:
    st.title(APP_TITLE)
    st.caption(APP_HINT)
    q = st.text_input('🔍 股票代码或名称', value='中国移动')
    limit = st.slider('历史期数', min_value=2, max_value=20,
                      value=int(qr.cfg_get('default_periods', 8)), step=1)
    go_btn = st.button('查询', type='primary', use_container_width=True)
    st.divider()
    st.caption('口径说明：利润/现金流默认单季(3个月可比)')
    st.caption('中报/年报同口径看同比，不混尺度')
    st.caption('数据仅供研究参考，非投资建议')

with st.spinner('正在拉取数据...'):
    ticker, ths, name, err = cached_resolve(q.strip())

if err or not ths:
    st.error(f'未找到标的：{q}')
    st.stop()

# ========== 主体：Tab 布局 ==========
st.title(f'{name}  ({ths})')
st.caption(f'更新于 {qr.datetime.now():%Y-%m-%d %H:%M}')

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    '📋 总览 Overview', '📈 利润表 Income Statement',
    '📊 资产负债表 Balance Sheet', '💵 现金流量表 Cash Flow',
    '🧩 概念板块 Concept Boards', '📰 券商研报 Research',
    '🧬 因子画像 Factor Profile', '🧮 估值判断 Valuation',
])

# ---------- Tab1 总览 Overview ----------
with tab1:
    snap = cached_snapshot(ths)
    if snap:
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric('最新价 Last', qr.fmt_num(snap.get('last_price')),
                  f"{qr.fmt_num(snap.get('price_change'), 2)} ({qr.fmt_pct(snap.get('price_change_ratio_pct'))})")
        c2.metric('今开 Open', qr.fmt_num(snap.get('open_price')))
        c3.metric('最高/最低 H/L', f"{qr.fmt_num(snap.get('high_price'))} / {qr.fmt_num(snap.get('low_price'))}")
        c4.metric('前收 Prev Close', qr.fmt_num(snap.get('prev_price')))
        c5.metric('成交额 Turnover', qr.fmt_yi(snap.get('turnover')))
        c6.metric('数据时间 Time', qr.datetime.now().strftime('%H:%M'))

    val = cached_valuations(ths)
    if val:
        st.subheader('估值 Valuation')
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric('PE (TTM)', qr.fmt_num(val.get('pe_ttm')))
        c2.metric('PE (MRQ)', qr.fmt_num(val.get('pe_mrq')))
        c3.metric('PB (MRQ)', qr.fmt_num(val.get('pb_mrq')))
        c4.metric('PS (TTM)', qr.fmt_num(val.get('ps_ttm')))
        c5.metric('PCF (TTM)', qr.fmt_num(val.get('pcf_ttm')))

    st.subheader('收入与利润 Revenue & Profit（同口径）')
    r1, r2 = st.columns(2)
    with r1:
        sdf = single_quarter_df(ths, limit)
        if sdf is not None and not sdf.empty:
            render_bar_chart(sdf, ['单季营收', '单季归母净利'],
                             '单季营收 / 归母净利 Single Quarter（每格3个月）')
    with r2:
        hdf = h1_income_df(ths, limit)
        if hdf is not None and not hdf.empty:
            render_bar_chart(hdf, ['营业收入', '归母净利润'],
                             '中报 H1 历年同口径（营收/归母净利 YoY 直接可比）')

    rdf = income_ratio_df(ths, limit)
    if rdf is not None and not rdf.empty:
        render_line_chart(rdf, ['毛利率', '净利率'], '毛利率 / 净利率趋势 Margin Trend')
        render_line_chart(rdf, ['销售费用率', '管理费用率', '研发费用率'],
                          '费用率趋势 Expense Ratios')

    rows, cur_key, prev_key, ierr = qr.get_income_analysis(ths)
    if not ierr and cur_key and prev_key:
        allitems = ((cached_statement(ths, 'income', 10).get('data') or {}).get('item', []))
        tmap = {(it.get('fiscal_year'), it.get('fiscal_period')): it for it in allitems}
        ci, pi = tmap.get(cur_key), tmap.get(prev_key)
        if ci and pi:
            lbl = qr._period_label(*cur_key)
            st.info(f'**{lbl} 同比 YoY（同口径）** · 营收 Revenue '
                    f'{qr.fmt_pct(qr._pct(ci.get("operating_income"), pi.get("operating_income")))}, '
                    f'归母净利 Net Profit '
                    f'{qr.fmt_pct(qr._pct(ci.get("parent_holder_net_profit"), pi.get("parent_holder_net_profit")))}')

    st.subheader('财务指标 Financial Indicators')
    report, abilities, aerr = cached_indicators(ths)
    if abilities:
        st.caption(f'报告期 Report {report}')
        for cat, inds in abilities.items():
            with st.expander(cat, expanded=(cat == '盈利能力')):
                for k, v in inds.items():
                    en = _CN2EN.get(k, '')
                    st.markdown(f'**{k}** {en}  →  {qr.fmt_num(v, 2)}')
    else:
        st.warning(aerr)

# ---------- Tab2-4 三表明细（同口径） ----------
STATEMENT_TAB = {'income': (tab2, '📈 利润表 Income Statement'),
                 'balance': (tab3, '📊 资产负债表 Balance Sheet'),
                 'cashflow': (tab4, '💵 现金流量表 Cash Flow')}
CHART_COLS = {
    'income': [('operating_income', '营业收入 Revenue'), ('parent_holder_net_profit', '归母净利润 Net Profit')],
    'balance': [('assets_total', '资产 Total Assets'), ('total_debt', '负债 Total Liabilities'),
                ('holder_equity_total', '权益 Total Equity')],
    'cashflow': [('act_cash_flow_net', '经营 Operating'), ('invest_cash_flow_net', '投资 Investing'),
                 ('financing_cash_flow_net', '筹资 Financing')],
}

for st_key, (tab, title) in STATEMENT_TAB.items():
    with tab:
        is_flow = qr.STATEMENTS[st_key]['kind'] == 'flow'
        opts = ['single', 'h1', 'fy'] if is_flow else ['all', 'h1', 'fy']
        scope = st.radio('报告口径 Scale（只显示同尺度数据）', opts, index=0, horizontal=True,
                         format_func=lambda s: SCOPE_NAMES[s], key=f'scope_{st_key}')
        hint = ('每列都是3个月单季，环比=前一单季' if (is_flow and scope == 'single')
                else ('仅历年' + ('中报' if scope == 'h1' else '年报') + '，同比=去年同期'
                      if scope in ('h1', 'fy')
                      else '期末时点快照，可直接并列'))
        st.caption(f'**{title}** · {hint} · 同比 YoY 列已同口径')

        cdf = statement_chart(ths, st_key, limit, CHART_COLS[st_key], scope)
        if cdf is not None and not cdf.empty:
            render_bar_chart(cdf, [c[1] for c in CHART_COLS[st_key]], f'{title} 关键科目 Key Items')

        if st_key == 'balance' and scope != 'all':
            field, rlabel = 'debt_ratio', '资产负债率 Debt-to-Asset Ratio'
            j = cached_statement(ths, st_key, limit)
            items = ((j or {}).get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
            rr = []
            for it in items[:limit]:
                if qr.FP_ORDER.get(it.get('fiscal_period')) in ({'h1': 2, 'fy': 4}[scope],):
                    v = qr._field_value(field, it)
                    rr.append({'报告期': qr._period_label(it.get('fiscal_year'), it.get('fiscal_period')),
                               rlabel: round(v, 2) if v is not None else None})
            if rr:
                render_line_chart(pd.DataFrame(rr[::-1]), [rlabel], f'{rlabel} 趋势')

        with st.expander('📄 明细表 Detail'):
            df, series, serr = statement_df(ths, st_key, limit, scope)
            if serr:
                st.warning(serr)
            else:
                st.dataframe(df, width='stretch', hide_index=True)
                label = st.selectbox('📈 选择「科目」Line Item（自动出折线图）',
                                     list(series.keys()), index=0,
                                     key=f'item_{st_key}_{scope}')
                s = series.get(label)
                if s:
                    cdf2 = pd.DataFrame({'报告期': s['periods'],
                                         '值': pd.to_numeric(s['values'], errors='coerce')})
                    if s['kind'] == 'amount':
                        cdf2['值'] = cdf2['值'] / 1e8
                        unit = '亿元'
                    elif s['kind'] == 'ratio':
                        unit = '%'
                    else:
                        unit = ''
                    render_line_chart(cdf2, ['值'], f'{label} 趋势 Line Trend', unit)

# ---------- Tab5 概念板块 ----------
with tab5:
    st.subheader('所属概念板块 Concept Boards（东财）')
    boards = cached_concept_boards(ticker)
    if not boards:
        st.info('未获取到概念板块')
    else:
        ordered = sorted(boards, key=lambda b: 0 if b.get('precise') == '1' else 1)

        # ---- 类神经网络图式: 成分股(输入) → 概念(隐藏) → 本股(输出) ----
        st.markdown('**🧠 概念网络图 Concept Network** · 左→右：成分股 → 概念 → 本股')
        st.caption('节点可拖拽 / 滚轮缩放 / 悬浮看详情；**点击概念圆圈 → 弹出该概念成分股面板**；勾选多个概念可看交叉成分股')
        gopts = [f"{b['code']} {b['name']}" for b in ordered]
        pick = st.multiselect('展开哪些概念（默认前5个）', gopts,
                              default=gopts[:5], key='concept_pick')
        if pick:
            sel = tuple(b for b in ordered if f"{b['code']} {b['name']}" in pick)
            html = cached_concept_graph(f'{name} ({ticker})', sel, 5)
            st.components.v1.html(html, height=680, scrolling=True)
        else:
            st.caption('（未选择概念，图式不显示）')
        st.divider()

        # ---- 明细表: 板块列表 + 单板块成分股 ----
        bdf = pd.DataFrame([{'代码 Code': b['code'],
                             '板块名称 Board': b['name'],
                             '类型 Type': qr.board_type(b)} for b in ordered])
        st.dataframe(bdf, width='stretch', hide_index=True)
        opts = [f"{b['code']} {b['name']}（{qr.board_type(b)}）" for b in ordered]
        choice = st.selectbox('🧩 选择板块看成分股 Constituents', opts, key='board_choice')
        bc = choice.split(' ', 1)[0]
        fdf = cached_board_financials(bc, ticker)
        if fdf is None or fdf.empty:
            st.info('未获取到成分股')
        else:
            num_cols = [c for c in fdf.columns if c != '名称']
            c1, c2 = st.columns([2, 1])
            with c1:
                sort_col = st.selectbox('📊 按指标排序 Sort by', num_cols,
                                        index=num_cols.index('总市值(亿)'), key='fin_sort')
            with c2:
                asc = st.radio('方向 Order', ['降序 Desc', '升序 Asc'],
                               index=0, horizontal=True, key='fin_asc')
            sdf = fdf.sort_values(sort_col, ascending=(asc == '升序 Asc'),
                                  na_position='last')
            st.dataframe(sdf, width='stretch', hide_index=True)
            st.caption('市值/PE/PB 来自东财；毛利率/营收增速/净利增速/ROE 取市值前12(同花顺，逐个调接口首次稍慢)')

# ---------- Tab6 研报 ----------
with tab6:
    st.subheader('最近券商研报 Research Reports（东财）')
    reports = cached_reports(ticker)
    if not reports:
        st.info('未获取到研报')
    else:
        rr = []
        for it in reports:
            rr.append({
                '标题 Title': it.get('title', ''),
                '机构 Org': it.get('orgSName', ''),
                '日期 Date': str(it.get('publishDate', ''))[:10],
                '评级 Rating': it.get('emRatingName', '') or '-',
            })
        st.dataframe(pd.DataFrame(rr), width='stretch', hide_index=True)
        # 下拉选择研报 → 摘要 + 去重下载（可靠切换）
        options = [f"{i + 1}. {r.get('title', '')}｜{r.get('orgSName', '')}｜{str(r.get('publishDate', ''))[:10]}"
                   for i, r in enumerate(reports)]
        choice = st.selectbox('📄 选择研报看摘要（可下载 PDF）', options, key='report_choice')
        idx = options.index(choice)
        rep = reports[idx]
        title = rep.get('title', '')
        st.divider()
        st.markdown(f'**{title}**  ·  {rep.get("orgSName", "")}  ·  '
                    f'{str(rep.get("publishDate", ""))[:10]}  ·  {rep.get("emRatingName", "") or ""}')
        abstract, pdf = cached_report_detail(rep.get('infoCode', ''))
        if abstract:
            st.markdown(abstract[:600])
        else:
            st.caption('（未抓到摘要）')
        if pdf:
            _dl_dir = str(st.session_state.get('last_dl_dir') or REPORTS_DIR)
            _p = Path(_dl_dir) / f'{_safe_stem(title)}.pdf'
            if _p.exists():
                st.success(f'✅ 该篇已下载过：`{_p}`（可重新选位置下载）')
            if st.button('⬇️ 下载这篇 PDF', type='primary'):
                download_report_dialog(title, pdf)
        else:
            st.caption('（无 PDF 链接）')

# ---------- Tab7 因子画像（大师关注点 → A股因子 总结） ----------
with tab7:
    st.subheader('因子画像 Factor Profile（大师关注点 → A股因子）')
    st.caption('把巴菲特/格雷厄姆/林奇/费雪关注的指标提炼为 A 股口径可计算因子；'
               '因子=连续画像（强弱），非买卖结论')
    flimit = max(limit, 16)
    inc = ((cached_statement(ths, 'income', flimit).get('data') or {}).get('item', []))
    bal = ((cached_statement(ths, 'balance', flimit).get('data') or {}).get('item', []))
    csh = ((cached_statement(ths, 'cashflow', flimit).get('data') or {}).get('item', []))
    val = cached_valuations(ths)
    # 同业参照: 用第一个精准概念板块的成分股中位数(需 pe/pb)
    peer = None
    boards = cached_concept_boards(ticker)
    if boards:
        pc = sorted(boards, key=lambda b: 0 if b.get('precise') == '1' else 1)[0]
        stocks = cached_board_stocks(pc['code'])
        pes = sorted(s['pe'] for s in stocks
                     if isinstance(s.get('pe'), (int, float)) and s['pe'] and s['pe'] > 0)
        pbs = sorted(s['pb'] for s in stocks
                     if isinstance(s.get('pb'), (int, float)) and s['pb'] and s['pb'] > 0)
        if pes and pbs:
            peer = {'name': pc['name'], 'n': len(pes),
                    'pe_median': round(pes[len(pes) // 2], 1),
                    'pb_median': round(pbs[len(pbs) // 2], 1)}
    prof = qr.build_factor_profile(inc, bal, csh, val, peer)

    render_radar(prof['radar'])
    st.divider()

    # 因子卡片
    _RC = {'积极': '🟢', '中性': '🟡', '偏弱': '🔴', '数据不足': '⚪'}
    for f in prof['factors']:
        with st.expander(f"{_RC.get(f['rating'], '')} {f['name']} {f['en']}  →  {f['value']}",
                         expanded=(f['key'] in ('roe', 'peg', 'debt'))):
            st.markdown(f"**{f['value']}** · {f['detail']}")
            st.caption(f"🧭 关注来源：{f['source']} ｜ 为什么看它：{f['why']}")
    st.divider()

    # 营收×净利矩阵 + 风险扫描
    m = prof['matrix']
    st.markdown('**营收×净利矩阵（年报同比）** 营收 '
                f"{qr.fmt_pct(m.get('rev')) if m.get('rev') is not None else '-'}"
                ' × 净利 '
                f"{qr.fmt_pct(m.get('np')) if m.get('np') is not None else '-'}")
    risk = prof['risk']
    if risk:
        st.warning('⚠️ 风险点：' + '；'.join(risk))
    else:
        st.success('未发现明显风险点')

    # 同业参照
    p = prof['peer']
    if p and p.get('n'):
        st.markdown(f"**同业参照**：同板块 **{p.get('name', '')}**（{p['n']} 家）"
                    f"PE 中位 {p.get('pe_med')} vs 本股 {p.get('pe_stock')}"
                    f"（{qr.fmt_pct(p.get('pe_vs')) if p.get('pe_vs') is not None else '-'}）"
                    f" · PB 中位 {p.get('pb_med')} vs 本股 {p.get('pb_stock')}"
                    f"（{qr.fmt_pct(p.get('pb_vs')) if p.get('pb_vs') is not None else '-'}）")
    st.caption('数据源：同花顺三表/估值 + 东财概念成分股 · 仅供研究参考，非投资建议')


# ---------- Tab8 估值判断 Valuation ----------
with tab8:
    st.subheader('🧮 估值判断 Valuation（横向 · 主营相似度 · 自身历史）')
    st.caption('三维度判断当前估值位置：① 跟行业/概念比 ② 找业务结构最像的同行 ③ 跟自己历史比。'
               '**估值是筛选参考，不是定价**——可比性请以主营构成为准。')
    boards = cached_concept_boards(ticker)
    if not boards:
        st.info('未获取到概念板块')
    else:
        vordered = sorted(boards, key=lambda b: 0 if b.get('precise') == '1' else 1)
        vopts = [f"{b['code']} {b['name']}（{qr.board_type(b)}）" for b in vordered]
        base = st.selectbox('🏷️ 估值基准板块（行业 / 概念）', vopts, key='val_base')
        bc = base.split(' ', 1)[0]

        # ---- ① 横向: 本股 vs 板块估值分布 ----
        stats = cached_board_valuation(bc)
        val = cached_valuations(ths)
        if stats and val:
            pe_s, pb_s = stats.get('pe'), stats.get('pb')
            cur_pe, cur_pb = val.get('pe_ttm'), val.get('pb_mrq')
            bname = base.split(' ', 1)[1].split('（')[0]
            st.markdown(f'**① 横向：本股 vs 板块「{bname}」**')
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric('本股 PE', f"{cur_pe:.1f}" if cur_pe else '-')
            c2.metric('板块 PE 中位', f"{pe_s['median']}" if pe_s else '-')
            c3.metric('板块 PE P25', f"{pe_s['p25']}" if pe_s else '-')
            c4.metric('板块 PE P75', f"{pe_s['p75']}" if pe_s else '-')
            if pe_s and cur_pe:
                pos = '📈 高于 P75（板块内偏贵）' if cur_pe > pe_s['p75'] else (
                    '📉 低于 P25（板块内偏便宜）' if cur_pe < pe_s['p25'] else '➖ 板块中位区间')
            else:
                pos = '-'
            c5.metric('PE 位置', pos)
            c1.metric('本股 PB', f"{cur_pb:.2f}" if cur_pb else '-')
            c2.metric('板块 PB 中位', f"{pb_s['median']}" if pb_s else '-')
            c3.metric('板块 PB P25', f"{pb_s['p25']}" if pb_s else '-')
            c4.metric('板块 PB P75', f"{pb_s['p75']}" if pb_s else '-')
            if pb_s and cur_pb:
                pos = '📈 高于 P75（板块内偏贵）' if cur_pb > pb_s['p75'] else (
                    '📉 低于 P25（板块内偏便宜）' if cur_pb < pb_s['p25'] else '➖ 板块中位区间')
            else:
                pos = '-'
            c5.metric('PB 位置', pos)
            st.caption(f"板块成分股 {stats['pe']['n'] if pe_s else 0} 家（东财）；"
                       '用中位数避免微利/亏损股拉高均值')
        st.divider()

        # ---- ② 主营相似度: 找可比组 ----
        st.markdown('**② 主营相似度：业务结构最像的同行（可比组候选）**')
        st.caption('相似度 = 主营收入占比结构余弦（剔除"其他/合计"汇总项）；只做排序参考，'
                   '**最终可比性请以每家主营构成为准**（点开看差异）')
        vpeers = cached_board_stocks(bc)
        if vpeers:
            top_peers = tuple((s['code'], s['name']) for s in vpeers[:15])
            sims = cached_similarity(ths, top_peers)
            others = [x for x in sims if str(x['code']) != ticker][:5]
            if others:
                for r in others:
                    with st.expander(f"🔗 {r['name']} · 主营相似度 {r['sim']:.2f}"):
                        if r['structure']:
                            for x in r['structure'][:6]:
                                extra = (f"，毛利率 {(x['gross_margin'] or 0) * 100:.1f}%"
                                         if x.get('gross_margin') else '')
                                st.markdown(f"- {x['name']}：占比 {(x['ratio'] or 0) * 100:.1f}%{extra}")
                        else:
                            st.caption('（未取到主营构成）')
            else:
                st.caption('（板块内无其他成分股可对比）')
        st.divider()

        # ---- ③ 自身历史分位 ----
        st.markdown('**③ 自身历史：近5年 PE/PB 分位（当前处于什么位置）**')

        def _hpos(cur, qs):
            if cur is None or not qs:
                return '-'
            if cur <= qs[10]:
                return '🔻 历史低位（≤P10）'
            if cur >= qs[90]:
                return '🔺 历史高位（≥P90）'
            if cur >= qs[75]:
                return '📈 中高位'
            if cur <= qs[25]:
                return '📉 中低位'
            return '➖ 中位'

        h = cached_val_history(ths)
        if h:
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric('当前 PE', f"{h['current_pe']}" if h['current_pe'] else '-')
            c2.metric('P10', f"{h['pe'][10]}")
            c3.metric('P25', f"{h['pe'][25]}")
            c4.metric('P50', f"{h['pe'][50]}")
            c5.metric('P75', f"{h['pe'][75]}")
            c6.metric('P90', f"{h['pe'][90]}")
            if h['current_pe'] and h['pe'][90] > h['pe'][10]:
                pv = min(max((h['current_pe'] - h['pe'][10]) / (h['pe'][90] - h['pe'][10]), 0), 1)
                st.progress(pv, text=f"PE 位置：{_hpos(h['current_pe'], h['pe'])}（{pv * 100:.0f}% 档）")
            c1.metric('当前 PB', f"{h['current_pb']}" if h.get('current_pb') else '-')
            if h.get('pb'):
                c2.metric('P10', f"{h['pb'][10]}")
                c3.metric('P25', f"{h['pb'][25]}")
                c4.metric('P50', f"{h['pb'][50]}")
                c5.metric('P75', f"{h['pb'][75]}")
                c6.metric('P90', f"{h['pb'][90]}")
                if h['current_pb'] and h['pb'][90] > h['pb'][10]:
                    pv = min(max((h['current_pb'] - h['pb'][10]) / (h['pb'][90] - h['pb'][10]), 0), 1)
                    st.progress(pv, text=f"PB 位置：{_hpos(h['current_pb'], h['pb'])}（{pv * 100:.0f}% 档）")
            st.caption(f"近 {h['years']} 年 · {h['n']} 个周K样本（腾讯不复权价）")
            st.caption('⚠️ 局限：PE 分位用**当前 EPS** 近似历史——盈利大幅增长的周期股会被系统性低估'
                       '（如紫金矿业），参考性低；**PB 用当前净资产近似，变化慢、较可靠**。')
        else:
            st.info('未取到历史估值数据（可能标的无历史K线）')
        st.caption('数据源：东财板块成分股估值/主营构成 + 腾讯历史K线 + 同花顺估值 · 仅供研究参考，非投资建议')
