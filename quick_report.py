# -*- coding: utf-8 -*-
"""
快速看财报 - quick_report.py
============================
输入股票代码或名称, 输出一份「财报速览」:
  1. 基本资料 + 最新行情快照                    (同花顺 hithink-finance)
  2. 利润表速览: 最近期 + 去年同期, 自动算同比   (同花顺 hithink-finance)
  3. 财务指标: 成长/盈利/偿债/营运/现金流五类    (同花顺 hithink-finance)
  4. 估值快照: PE/PB/PS/PCF                     (同花顺 hithink-finance)
  5. 券商研报最近若干条(标题/机构/日期/评级)     (东财 reportapi, 免key)

用法:
  python quick_report.py 600941              总览(行情/估值/利润表速览/指标/研报)
  python quick_report.py 中国移动            支持按名称
  python quick_report.py 600941 -t income    利润表明细(历史多期+同比+环比)
  python quick_report.py 600941 -t balance   资产负债表明细
  python quick_report.py 600941 -t cashflow  现金流量表明细
  python quick_report.py 600941 -t all       总览 + 三表明细
  python quick_report.py 600941 -n 8         明细历史期数(默认6)

对比维度:
  - 同比: 最新累计期 vs 去年同期累计
  - 环比: 利润表/现金流 = 最新单季 vs 上一单季(由累计期推导);
          资产负债表 = 本期期末 vs 上期期末

API Key 配置(优先级从高到低):
  1. 环境变量 HITHINK_FINANCE_API_KEY
  2. 本文件同目录 hithink_config.json 里的 "api_key" 字段
  3. 运行时提示输入(getpass)

注意:
  - 同花顺接口有请求配额, 脚本内置 0.6s 节流 + 429 指数退避重试
  - 财务指标可能滞后于最新财报, 会自动回退到最近可用报告期
  - 研报来自东财 reportapi, 无 key, 仅供学习研究
"""

import sys
import os
import json
import re
import time
import getpass
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import requests

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ========== 配置 ==========
BASE = 'https://fuyao.aicubes.cn'
CONFIG_PATH = Path(__file__).resolve().parent / 'hithink_config.json'
THS_THROTTLE = 0.6      # 同花顺请求间隔(秒)
EM_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ========== API Key 解析 ==========
API_KEY = os.environ.get('HITHINK_FINANCE_API_KEY')
if not API_KEY and CONFIG_PATH.exists():
    try:
        API_KEY = json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('api_key')
    except Exception:
        pass
if not API_KEY:
    API_KEY = getpass.getpass('请输入同花顺 API Key: ')

# ========== 平台配置加载 (config.json, 可被环境变量覆盖) ==========
_APP_DIR = Path(__file__).resolve().parent
_CFG = {}
_cfg_path = _APP_DIR / 'config.json'
if _cfg_path.exists():
    try:
        _CFG = json.loads(_cfg_path.read_text(encoding='utf-8')) or {}
    except Exception:
        _CFG = {}


def cfg_get(key, default=None):
    """取配置: 环境变量 > config.json > 默认值"""
    env_map = {'api_key': 'HITHINK_FINANCE_API_KEY', 'reports_dir': 'STOCKREPORT_REPORTS_DIR',
               'default_periods': 'STOCKREPORT_DEFAULT_PERIODS'}
    env = env_map.get(key)
    if env and os.environ.get(env):
        return os.environ[env]
    return _CFG.get(key, default)

# ========== 工具函数 ==========
def ths_get(path, params, retries=4):
    """同花顺 GET, 带节流 + 429 退避重试"""
    time.sleep(THS_THROTTLE)
    url = BASE + path
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params,
                             headers={'X-api-key': API_KEY},
                             timeout=20)
            j = r.json()
            code = j.get('code')
            if code == 429:                      # 限流
                wait = 2 ** attempt
                print(f'  [同花顺限流429] {wait}s 后重试...')
                time.sleep(wait)
                continue
            return j
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f'  [同花顺请求失败] {e} {wait}s 后重试...')
            time.sleep(wait)
    return {'code': -1, 'message': '请求多次失败', 'data': None}


def fmt_yi(v, nd=2):
    """数值(元) → 亿字符串"""
    if v is None:
        return '-'
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '-'
    if abs(v) >= 1e12:
        return f'{v/1e12:.{nd}f}万亿'
    if abs(v) >= 1e8:
        return f'{v/1e8:.{nd}f}亿'
    if abs(v) >= 1e4:
        return f'{v/1e4:.{nd}f}万'
    return f'{v:.{nd}f}'


def fmt_pct(v):
    """百分比数值 → 'xx.x%'"""
    if v is None:
        return '-'
    try:
        return f'{float(v):+.2f}%' if abs(float(v)) < 10000 else f'{float(v):+.1f}%'
    except (TypeError, ValueError):
        return '-'


def fmt_num(v, nd=2):
    if v is None:
        return '-'
    try:
        return f'{float(v):.{nd}f}'
    except (TypeError, ValueError):
        return '-'


def now_ms():
    return int(datetime.now().timestamp() * 1000)


def _guess_thscode(ticker):
    if ticker.startswith(('6', '9')):
        return ticker + '.SH'
    if ticker.startswith(('0', '3')):
        return ticker + '.SZ'
    if ticker.startswith(('4', '8')):
        return ticker + '.BJ'
    return None


def _period_label(fy, fp):
    q = FP_ORDER.get(fp)
    return f'{fy}{FP_NAME.get(q, fp)}' if q else f'{fy}{fp}'


def _tbl(headers, rows):
    """用 tabulate 输出表格(已安装), 失败则退化到简单打印"""
    try:
        from tabulate import tabulate
        return tabulate(rows, headers=headers, tablefmt='simple', stralign='left',
                        numalign='right')
    except Exception:
        w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
        lines = ['  '.join(h.ljust(w[i]) for i, h in enumerate(headers)),
                 '  '.join('-' * w[i] for i in range(len(headers)))]
        for r in rows:
            lines.append('  '.join(str(c).ljust(w[i]) for i, c in enumerate(r)))
        return '\n'.join(lines)


# ========== 1. 标的解析 ==========
def resolve_stock(q):
    """输入 代码/名称 → (ticker, thscode, name, err)"""
    q = q.strip()
    m = re.fullmatch(r'(\d{6})(?:\.(SH|SZ|BJ))?', q, re.I)
    if m:
        ticker = m.group(1)
        ex = (m.group(2) or '').upper()
        ths = f'{ticker}.{ex}' if ex else _guess_thscode(ticker)
        if not ths:
            return None, None, None, f'无法推断 {ticker} 的交易所'
        name = ''
        j = ths_get('/api/meta/tickers/search', {'q': ticker, 'limit': 5})
        for it in (j.get('data') or {}).get('item', []):
            if it.get('thscode') == ths:
                name = it.get('name', '')
                break
        return ticker, ths, name or q, None
    # 名称检索
    j = ths_get('/api/meta/tickers/search', {'q': q, 'limit': 8})
    for it in (j.get('data') or {}).get('item', []):
        if it.get('asset_type') == 'a-share':
            return it.get('ticker'), it.get('thscode'), it.get('name') or q, None
    return None, None, None, f'未找到标的: {q}'


# ========== 2. 行情 + 估值 ==========
def get_snapshot(thscode):
    j = ths_get('/api/a-share/prices/snapshot', {'thscodes': thscode})
    if not j or j.get('code') != 0:
        return None
    items = (j.get('data') or {}).get('item', [])
    return items[0] if items else None


def get_valuations(thscode):
    j = ths_get('/api/a-share/valuations/snapshot', {'thscodes': thscode})
    if not j or j.get('code') != 0:
        return None
    items = (j.get('data') or {}).get('item', [])
    return items[0] if items else None


# ========== 3. 利润表分析(自动同比) ==========
def get_income_analysis(thscode, limit=10):
    """返回 (rows, cur_key, prev_key, err)
    rows: [(fiscal_year, fiscal_period, 营收, 归母净利, EPS, 报告期), ...]
    """
    j = ths_get('/api/a-share/financials/income-statements',
                {'thscode': thscode, 'period': 'quarterly', 'limit': limit})
    if not j:
        return [], None, None, '请求失败'
    if j.get('code') != 0:
        return [], None, None, f"同花顺错误: {j.get('message')}"
    items = (j.get('data') or {}).get('item', [])
    if not items:
        return [], None, None, '无利润表数据'
    table = {}
    rows = []
    for it in items:
        key = (it.get('fiscal_year'), it.get('fiscal_period'))
        table[key] = it
        rows.append((it.get('fiscal_year'), it.get('fiscal_period'),
                     it.get('operating_income'), it.get('parent_holder_net_profit'),
                     it.get('basic_eps')))
    cur = items[0]                     # 最新(降序第一)
    cur_key = (cur.get('fiscal_year'), cur.get('fiscal_period'))
    prev_key = (cur_key[0] - 1, cur_key[1]) if cur_key[1] else None
    return rows, cur_key, prev_key, None


def _pct(new, old):
    if new is None or old in (None, 0):
        return None
    try:
        return (float(new) - float(old)) / abs(float(old)) * 100
    except (TypeError, ValueError):
        return None


# ========== 4. 财务指标(自动回退报告期) ==========
ABILITY_NAMES = {
    'growth': '成长能力', 'profitability': '盈利能力',
    'solvency': '偿债能力', 'operation': '营运能力', 'cash-flow': '现金流',
}
INDICATOR_LABELS = {
    'calculate_operating_income_yoy_growth_ratio': ('营收同比', 'Revenue YoY'),
    'calculate_operating_profit_yoy_growth_ratio': ('营业利润同比', 'Operating Profit YoY'),
    'calculate_parent_holder_net_profit_yoy_growth_ratio': ('归母净利同比', 'Net Profit YoY'),
    'total_assets_growth_ratio': ('总资产同比', 'Total Assets YoY'),
    'fixed_asset_invest_expansion_ratio': ('固定资产扩张率', 'Fixed Asset Expansion'),
    'index_weighted_avg_roe': ('加权ROE', 'Weighted ROE'),
    'index_deduct_weighted_avg_roe': ('扣非ROE', 'ROE excl. Non-recurring'),
    'sale_gross_margin': ('销售毛利率', 'Gross Margin'),
    'sale_net_interest_ratio': ('销售净利率', 'Net Margin'),
    'total_assets_net_ratio': ('总资产净利率', 'ROA'),
    'assets_debt_ratio': ('资产负债率', 'Debt-to-Asset Ratio'),
    'current_ratio': ('流动比率', 'Current Ratio'),
    'quick_ratio': ('速动比率', 'Quick Ratio'),
    'cash_ratio': ('现金比率', 'Cash Ratio'),
    'earned_interest_multiple': ('已获利息倍数', 'Interest Coverage'),
    'total_assets_turnover_ratio': ('总资产周转率', 'Asset Turnover'),
    'inventory_turnover_ratio': ('存货周转率', 'Inventory Turnover'),
    'current_assets_turnover_ratio': ('流动资产周转率', 'Current Asset Turnover'),
    'receive_account_turnover_ratio': ('应收账款周转率', 'Receivable Turnover'),
    'long_term_debt_equity_ratio': ('长期债务权益比', 'LT Debt/Equity'),
    'net_profit_cash_content': ('净利润现金含量', 'Cash Content of Profit'),
    'cash_operating_index': ('经营现金指数', 'Operating Cash Index'),
    'operating_cash_flow_net_divide_income': ('经营现金流/净利润', 'OCF/Net Profit'),
    'cash_meet_invest_ratio': ('现金满足投资比率', 'Cash/Investment'),
}


def _indicator_candidates():
    """按当前月份生成候选报告期(最近优先)"""
    y = datetime.now().year
    m = datetime.now().month
    if m >= 11:
        latest = [(y, 4)]
    elif m >= 9:
        latest = [(y, 3)]
    elif m >= 5:
        latest = [(y, 2)]
    else:
        latest = [(y - 1, 4)]
    cands = latest + [(y - 1, 4), (y, 1), (y - 1, 2), (y - 1, 3)]
    seen, out = set(), []
    for yy, qq in cands:
        c = f'{yy}-{qq}'
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def get_indicators_latest(thscode):
    """返回 (report, abilities_dict, err) — abilities_dict: {ability: {label: value}}"""
    for report in _indicator_candidates():
        j = ths_get('/api/a-share/financials/indicators',
                    {'thscode': thscode, 'report': report})
        if not j or j.get('code') != 0:
            continue                     # 429/5003(空)/其他 → 试下一个
        abilities = (j.get('data') or {}).get('abilities', [])
        if not abilities:
            continue
        result = {}
        for ab in abilities:
            name = ab.get('ability', '')
            items = ab.get('indicators', [])
            mapped = {}
            for it in items:
                iid = it.get('index_id', '')
                mapped[INDICATOR_LABELS.get(iid, (iid, iid))[0]] = it.get('value')
            result[ABILITY_NAMES.get(name, name)] = mapped
        return report, result, None
    return None, {}, '财务指标暂不可用(报告期未算好或超配额)'


def _to_float(v):
    """把 API 返回的数值(可能是字符串/'--'/None)安全转成 float，失败返回 None"""
    try:
        f = float(v)
        return f if f == f else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def get_financial_core(thscode):
    """单股核心财务(同花顺): {gross_margin, rev_yoy, np_yoy, roe}; 失败返回 None
    从五类财务指标里取 毛利率/营收同比/归母净利同比/加权ROE
    注意: 指标原始值可能是字符串，必须转 float 才能正确排序/绘图"""
    _report, abilities, _err = get_indicators_latest(thscode)
    if not abilities:
        return None
    out = {'gross_margin': None, 'rev_yoy': None, 'np_yoy': None, 'roe': None}
    targets = {'销售毛利率': 'gross_margin', '营收同比': 'rev_yoy',
               '归母净利同比': 'np_yoy', '加权ROE': 'roe'}
    for ab in abilities.values():
        for k, v in ab.items():
            if k in targets and out[targets[k]] is None:
                out[targets[k]] = _to_float(v)
    return out


# ========== 5. 东财研报(免key) ==========
def get_reports_eastmoney(ticker, page_size=8, begin='2025-01-01'):
    """东财 reportapi 个股研报列表"""
    end = datetime.now().strftime('%Y-%m-%d')
    body = {'pageSize': page_size, 'pageNo': 1,
            'beginTime': begin, 'endTime': end, 'code': ticker}
    try:
        r = requests.post('https://reportapi.eastmoney.com/report/list2',
                          json=body,
                          headers={'Content-Type': 'application/json',
                                   'User-Agent': EM_UA},
                          timeout=20)
        j = r.json()
        items = j.get('data') or []
        return items if isinstance(items, list) else []
    except Exception as e:
        print(f'  [研报获取失败] {e}')
        return []


def get_report_detail(info_code):
    """抓东财研报详情页 → (摘要, pdf_url)"""
    if not info_code:
        return '', ''
    url = 'https://data.eastmoney.com/report/zw_industry.jshtml?infocode=' + info_code
    try:
        r = requests.get(url, headers={'User-Agent': EM_UA}, timeout=20)
        html = r.text
        ps = re.findall(r'<p[^>]*>(.*?)</p>', html, re.S)
        txt = ''.join(re.sub(r'<[^>]+>', '', p) for p in ps)
        txt = re.sub(r'\s+', ' ', txt).strip()
        m = re.search(r'https://pdf\.dfcfw\.com/pdf/[^\s"\'<>]+?\.pdf', html)
        pdf = m.group(0) if m else ''
        return txt, pdf
    except Exception:
        return '', ''


_CHALLENGE_SHIM = """
var __c = [];
var document = {};
Object.defineProperty(document, 'cookie', {
  get: function(){ return __c.join('; '); },
  set: function(v){ __c.push(v); }
});
var location = { reload: function(){} };
var window = { location: location };
var navigator = { userAgent: 'Mozilla/5.0' };
var setTimeout = function(fn, ms){};
"""


def download_report_pdf(pdf_url, save_path):
    """下载研报PDF(带 %PDF 校验), 返回是否成功
    东财 pdf.dfcfw.com 有反爬: 首次请求返回混淆 JS 挑战页(算 EO_Bot_Ssid cookie)。
    用 py_mini_racer(V8) 执行该 JS 取回 cookie, 带 cookie 重试拿到真 PDF。
    """
    s = requests.Session()
    s.headers.update({'User-Agent': EM_UA, 'Accept-Language': 'zh-CN,zh;q=0.9',
                      'Referer': 'https://data.eastmoney.com/'})

    def _is_pdf(resp):
        return resp.status_code == 200 and resp.content[:4] == b'%PDF'

    def _save(resp):
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(resp.content)
        return True

    try:
        r = s.get(pdf_url, timeout=40)
        if _is_pdf(r):
            return _save(r)
        # 反爬挑战: 提取 JS 用 V8 执行, 算 cookie 后重试
        m = re.search(r'<script>(.*?)</script>', r.text, re.S)
        cookies = ''
        if m and ('EO_Bot_Ssid' in r.text or 'document' in r.text):
            try:
                from py_mini_racer import MiniRacer
                ctx = MiniRacer()
                ctx.eval(_CHALLENGE_SHIM)
                ctx.eval(m.group(1))
                cookies = ctx.eval('document.cookie')
            except Exception:
                cookies = ''
        if cookies:
            r2 = s.get(pdf_url, headers={'Cookie': cookies,
                                         'Referer': 'https://data.eastmoney.com/'},
                       timeout=40)
            if _is_pdf(r2):
                return _save(r2)
    except Exception:
        pass
    return False


# ========== 5.5 概念板块(东财,免key) ==========
def _f10_code(ticker):
    """ticker(600941) → 东财F10代码(SH600941 / SZ000001 / BJxxxxxx)"""
    t = str(ticker).strip().zfill(6)
    if t.startswith(('6', '9')):
        return 'SH' + t
    if t.startswith(('0', '3')):
        return 'SZ' + t
    return 'BJ' + t


def get_concept_boards(ticker, maxn=60):
    """个股所属板块(概念/行业/风格/指数) → [{code,name,rank,precise}]"""
    url = ('https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/'
           f'PageAjax?code={_f10_code(ticker)}')
    try:
        r = requests.get(url, headers={'User-Agent': EM_UA}, timeout=20)
        ss = (r.json() or {}).get('ssbk') or []
        out = []
        for it in ss[:maxn]:
            out.append({
                'code': str(it.get('BOARD_CODE', '')),
                'name': it.get('BOARD_NAME', ''),
                'rank': str(it.get('BOARD_RANK', '')),
                'precise': str(it.get('IS_PRECISE') or ''),
            })
        return out
    except Exception as e:
        print(f'  [概念板块获取失败] {e}')
        return []


def board_type(b):
    """板块类型: 概念(精准) / 行业指数 / 其他"""
    p = (b or {}).get('precise', '')
    return '概念' if p == '1' else ('行业/指数' if p == '0' else '其他')


def get_board_stocks(board_code, limit=200):
    """板块成分股(push2delay) → [{code,name,price,pct,mcap,pe,pb}], 按总市值降序
    board_code: F10 板块代码(数字), 会补零成 4 位 BK 代码(如 714 → BK0714)"""
    fs = f'b:BK{str(board_code).zfill(4)}'
    params = {'pn': 1, 'pz': limit, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
              'fid': 'f20', 'fs': fs, 'fields': 'f12,f14,f2,f3,f20,f9,f23'}
    try:
        r = requests.get('https://push2delay.eastmoney.com/api/qt/clist/get',
                         params=params,
                         headers={'User-Agent': EM_UA,
                                  'Referer': 'https://quote.eastmoney.com/'},
                         timeout=20)
        diff = ((r.json() or {}).get('data') or {}).get('diff') or []
        out = []
        for it in diff:
            out.append({'code': str(it.get('f12', '')),
                        'name': it.get('f14', ''),
                        'price': it.get('f2'),
                        'pct': it.get('f3'),
                        'mcap': it.get('f20'),
                        'pe': it.get('f9'),
                        'pb': it.get('f23')})
        return out
    except Exception as e:
        print(f'  [成分股获取失败] {e}')
        return []


_CONCEPT_PALETTE = ['#e63946', '#2a9d8f', '#3a86ff', '#e9c46a', '#8e7cc3',
                    '#f4a261', '#5fad56', '#fb8500', '#6d597a', '#457b9d']

# 概念图: 点击概念节点 → iframe 内弹出该概念成分股面板的 JS 模板
# __BOARD_JSON__ 会被替换为 {code: {name, stocks}} JSON
_PANEL_JS_TEMPLATE = r"""
<script>
window.BOARD_STOCKS = __BOARD_JSON__;
var __panel = null;
function __fyi(v){ if(v==null)return '-'; v=Number(v);
  if(Math.abs(v)>=1e12)return (v/1e12).toFixed(2)+'万亿';
  if(Math.abs(v)>=1e8)return (v/1e8).toFixed(1)+'亿';
  return v.toFixed(0); }
function __showPanel(code,name){
  if(__panel){ __panel.remove(); }
  var d = window.BOARD_STOCKS[code]; if(!d) return;
  var rows = d.stocks.map(function(s){
    return '<tr><td style="padding:3px 8px;border-bottom:1px solid #f1f5f9">'+s[1]+'</td>'
      + '<td style="padding:3px 8px;border-bottom:1px solid #f1f5f9;color:#64748b">'+s[0]+'</td>'
      + '<td style="padding:3px 8px;border-bottom:1px solid #f1f5f9;text-align:right;color:#334155">'+__fyi(s[4])+'</td></tr>';
  }).join('');
  __panel = document.createElement('div');
  __panel.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:99;width:310px;'
    + 'max-height:62%;overflow:auto;background:rgba(255,255,255,.98);border:1px solid #e2e8f0;'
    + 'border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.12);font:12px "Microsoft YaHei";color:#1f2937';
  __panel.innerHTML = '<div style="padding:8px 12px;font-weight:700;color:#0f172a;'
    + 'border-bottom:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center">'
    + name + '（前' + d.stocks.length + '大市值）'
    + '<span style="cursor:pointer;color:#94a3b8" onclick="__hidePanel()">✕</span></div>'
    + '<table style="width:100%;border-collapse:collapse"><tbody>' + rows + '</tbody></table>';
  document.body.appendChild(__panel);
}
function __hidePanel(){ if(__panel){ __panel.remove(); __panel=null; } }
window.addEventListener('load', function(){
  var net = window.network; if(!net) return;
  net.on('click', function(p){
    var ids = p && p.nodes;
    if(ids && ids.length){
      var id = ids[0], n = net.body.data.nodes.get(id);
      if(n && id.indexOf('c') === 0){ __showPanel(id.substring(1), n.label); }
    }
  });
});
</script>
"""

# 概念图动态效果: 入场浮现 + 悬停邻居高亮/其余淡出(平滑缓动)
_DYNAMIC_JS_TEMPLATE = r"""
<script>
(function(){
  var net = null;
  function baseSize(id){
    var s = String(id).charAt(0);
    return s === 's' ? 7 : (s === 'c' ? 26 : 46);
  }
  function applySmooth(updates, ms){
    var t0 = performance.now();
    function step(){
      var p = Math.min(1, (performance.now()-t0)/ms);
      var e = 1 - Math.pow(1-p, 3);
      var nodes = net.body.data.nodes;
      for(var i=0;i<updates.length;i++){
        var u = updates[i], n = nodes.get(u.id);
        if(!n) continue;
        nodes.update({id:u.id,
          size: u.from.size + (u.to.size-u.from.size)*e,
          opacity: u.from.opacity + (u.to.opacity-u.from.opacity)*e});
      }
      if(p < 1) setTimeout(step, 16);
    }
    setTimeout(step, 16);
  }
  function neighborsOf(id){
    var out = {}; out[id] = 1;
    var es = net.body.data.edges.get();
    for(var i=0;i<es.length;i++){ var e=es[i];
      if(e.from===id){ out[e.to]=1; } if(e.to===id){ out[e.from]=1; } }
    return out;
  }
  function dimOthers(centerId){
    var nb = neighborsOf(centerId);
    var updates = [];
    var nodes = net.body.data.nodes.get();
    for(var i=0;i<nodes.length;i++){
      var n = nodes[i];
      if(n.id === centerId) continue;
      var from = {size:n.size, opacity:(n.opacity==null?1:n.opacity)};
      if(nb[n.id]){ updates.push({id:n.id, from:from, to:{size:from.size*1.25, opacity:1}}); }
      else { updates.push({id:n.id, from:from, to:{size:from.size*0.85, opacity:0.15}}); }
    }
    var hn = net.body.data.nodes.get(centerId);
    if(hn){ updates.push({id:centerId, from:{size:hn.size, opacity:(hn.opacity==null?1:hn.opacity)},
                          to:{size:Math.min(hn.size*1.6, 70), opacity:1}}); }
    applySmooth(updates, 260);
  }
  function restore(){
    var updates = [];
    var nodes = net.body.data.nodes.get();
    for(var i=0;i<nodes.length;i++){
      var n = nodes[i];
      var bs = baseSize(n.id);
      var op = (n.opacity==null?1:n.opacity);
      if(Math.abs(n.size-bs) > 0.5 || op < 0.99){
        updates.push({id:n.id, from:{size:n.size, opacity:op}, to:{size:bs, opacity:1}});
      }
    }
    if(updates.length) applySmooth(updates, 260);
  }
  function entrance(){
    var nodes = net.body.data.nodes.get();
    var comps = [];
    for(var i=0;i<nodes.length;i++){
      if(String(nodes[i].id).charAt(0) === 's'){ comps.push(nodes[i].id); }
    }
    var t0 = performance.now();
    function step(){
      var p = Math.min(1, (performance.now()-t0)/1000);
      for(var i=0;i<comps.length;i++){
        net.body.data.nodes.update({id:comps[i], opacity:p, size:1+p*6});
      }
      if(p < 1) setTimeout(step, 16);
    }
    setTimeout(step, 16);
  }
  window.addEventListener('load', function(){
    net = window.network; if(!net) return;
    setTimeout(entrance, 120);
    net.on('hoverNode', function(p){ if(p && p.node) dimOthers(p.node); });
    net.on('blurNode', restore);
  });
})();
</script>
"""


def build_concept_graph_html(stock_label, boards, max_stocks=15, max_label=15):
    """概念网络图(connected-papers 风格) → pyvis HTML

    力导向布局(forceAtlas2) + 按概念聚类配色 + 悬停高亮邻居 + 平滑曲线。
    boards: [{code,name,...}]；每板块取市值前 max_stocks 只；全图市值前 max_label 只显示标签。
    点击概念节点 → iframe 内弹出该概念成分股面板(数据内嵌, 自包含)。
    """
    import json as _json
    from pyvis.network import Network
    net = Network(height='680px', width='100%', directed=False, bgcolor='#ffffff',
                  font_color='#1f2937')
    bname = {b['code']: b['name'] for b in boards}
    bcolor = {b['code']: _CONCEPT_PALETTE[i % len(_CONCEPT_PALETTE)]
              for i, b in enumerate(boards)}
    board_data = {}          # code -> {name, stocks: [[code,name,price,pct,mcap],...]}

    # 本股(居中大节点)
    root_col = bcolor[boards[0]['code']] if boards else '#e63946'
    net.add_node('stock', label=stock_label,
                 color={'background': root_col, 'border': '#7f1d1d'},
                 size=46, borderWidth=3, group='本股', title=stock_label,
                 font={'size': 20, 'color': '#7f1d1d'})
    # 概念节点(各自色系)
    for b in boards:
        cid = 'c' + b['code']
        col = bcolor[b['code']]
        net.add_node(cid, label=b['name'],
                     color={'background': col, 'border': '#ffffff'},
                     size=26, borderWidth=2, group=b['name'],
                     title=f'概念板块: {b["name"]}（{board_type(b)}）',
                     font={'size': 14, 'color': col})
        net.add_edge(cid, 'stock', width=1.0, color='#cbd5e1')
        board_data[b['code']] = {'name': b['name'], 'stocks': []}
    # 成分股(归到所属概念的颜色, 聚成团)
    seen = {}
    for b in boards:
        for s in get_board_stocks(b['code'], limit=max_stocks):
            board_data[b['code']]['stocks'].append(
                [str(s['code']), s['name'], s.get('price'), s.get('pct'), s.get('mcap')])
            sid = 's' + s['code']
            rec = seen.setdefault(sid, {'name': s['name'], 'mcap': s.get('mcap'),
                                        'code': s['code'], 'board': b['code'],
                                        'boards': []})
            rec['boards'].append(b['code'])
    ranked = sorted(seen.values(), key=lambda x: -(x['mcap'] or 0))
    top_names = {x['name'] for x in ranked[:max_label]}
    for sid, info in seen.items():
        col = bcolor.get(info['board'], '#94a3b8')
        net.add_node(sid, label=(info['name'] if info['name'] in top_names else ''),
                     color={'background': col, 'border': '#ffffff',
                            'highlight': {'background': col, 'border': '#1f2937'}},
                     size=7, borderWidth=1, group=bname.get(info['board'], '成分股'),
                     title=f"{info['name']} {info['code']}"
                           + (f' · 总市值 {fmt_yi(info["mcap"])}' if info.get('mcap') else ''),
                     font={'size': 10, 'color': '#475569'})
        for bc in info['boards']:
            net.add_edge(sid, 'c' + bc, width=0.6, color='#e2e8f0')

    options = {
        'physics': {'solver': 'forceAtlas2Based',
                    'forceAtlas2Based': {'gravitationalConstant': -40,
                                         'centralGravity': 0.012,
                                         'springLength': 100,
                                         'springConstant': 0.08,
                                         'damping': 0.4,
                                         'avoidOverlap': 0.2},
                    'stabilization': {'enabled': True, 'iterations': 250,
                                      'updateInterval': 20}},
        'interaction': {'hover': True, 'hoverConnectedEdges': True,
                        'tooltipDelay': 120, 'navigationButtons': False},
        'edges': {'smooth': {'type': 'continuous', 'roundness': 0.5},
                  'color': {'color': '#d1d5db', 'highlight': '#1f2937',
                            'hover': '#1f2937'},
                  'width': 0.8},
        'nodes': {'shape': 'dot', 'shadow': False,
                  'font': {'face': 'Microsoft YaHei', 'size': 12,
                           'color': '#374151'}},
    }
    net.set_options(_json.dumps(options, ensure_ascii=False))
    html = net.generate_html()
    # 手动图例(vis.js 新版已移除 legend 选项)
    legend_items = ('<div style="display:flex;align-items:center">'
                    f'<span style="width:11px;height:11px;border-radius:50%;'
                    f'background:{root_col};margin-right:6px;flex:none"></span>本股</div>')
    for b in boards:
        legend_items += (
            '<div style="margin-top:2px;display:flex;align-items:center">'
            f'<span style="width:11px;height:11px;border-radius:50%;'
            f'background:{bcolor[b["code"]]};margin-right:6px;flex:none"></span>'
            f'{b["name"]}</div>')
    legend_html = (
        '<div style="position:fixed;right:12px;bottom:12px;z-index:99;'
        'background:rgba(255,255,255,.95);border:1px solid #e5e7eb;'
        'border-radius:10px;padding:8px 12px;font:12px \'Microsoft YaHei\';'
        'color:#374151;box-shadow:0 2px 8px rgba(0,0,0,.08);line-height:1.7">'
        + legend_items + '</div>')
    # 点击概念节点 → iframe 内弹出该概念成分股面板(数据内嵌, 自包含)
    board_json = _json.dumps(board_data, ensure_ascii=False)
    panel_js = _PANEL_JS_TEMPLATE.replace('__BOARD_JSON__', board_json)
    return html.replace('</body>', legend_html + panel_js + _DYNAMIC_JS_TEMPLATE + '</body>')
    return html.replace('</body>', legend_html + panel_js + '</body>')


# ========== 8. 因子画像(大师关注点 → A股因子) ==========
def _annual_items(items):
    return [it for it in items if FP_ORDER.get(it.get('fiscal_period')) == 4]


def _match_year(items, year):
    for it in items:
        if it.get('fiscal_year') == year:
            return it
    return None


def _latest_two_annual(items, field):
    """最近两个年报该字段 → (cur, prev, cur_year)"""
    ann = sorted(_annual_items(items), key=lambda it: (it.get('fiscal_year') or 0))
    if not ann:
        return None, None, None
    cur = ann[-1]
    prev = ann[-2] if len(ann) > 1 else None
    return cur.get(field), (prev or {}).get(field), cur.get('fiscal_year')


def annual_roe_series(income_items, balance_items):
    """年报口径 ROE(简化): 归母净利 / 期末所有者权益 → [{year, roe}]
    注: 用总权益(含少数股东)近似归母权益, ROE 会略偏低, 属简化口径 """
    out = []
    ann_b = _annual_items(balance_items)
    for y in sorted({it.get('fiscal_year') for it in ann_b}):
        b = _match_year(ann_b, y)
        i = _match_year(_annual_items(income_items), y)
        eq = (b or {}).get('holder_equity_total')
        np_ = (i or {}).get('parent_holder_net_profit')
        if eq and np_:
            out.append({'year': y, 'roe': round(np_ / eq * 100, 1)})
    return out


def build_factor_profile(income_items, balance_items, cashflow_items, val, peer=None):
    """大师关注点 → A股口径因子画像
    income/balance/cashflow: 各表 item 列表(同花顺原始)
    val: 估值 dict(pe_ttm/pb_mrq/...)
    peer: {'name','n','pe_median','pb_median'} 同板块参照(可选)
    → {'radar': {维度:0-100}, 'factors': [dict], 'matrix': {...}, 'risk': [str], 'peer': {...}}
    """
    factors, risk = [], []
    radar = {'盈利质量': 0, '成长': 0, '价值': 0, '质量': 0, '安全': 0}

    def _f(key, n, en, src, why, v, r, d):
        return {'key': key, 'name': n, 'en': en, 'source': src, 'why': why,
                'value': v, 'rating': r, 'detail': d}

    # ---- 1. ROE 持续性 (巴菲特·质量/护城河) ----
    rs = annual_roe_series(income_items, balance_items)
    if rs:
        cur = rs[-1]['roe']
        ok_n = sum(1 for r in rs if r['roe'] >= 15)
        rating = '积极' if (cur >= 15 and ok_n >= 2) else ('中性' if cur >= 10 else '偏弱')
        factors.append(_f('roe', 'ROE 持续性', 'ROE Persistence', '巴菲特 · 质量/护城河',
                          '长期高 ROE ≈ 生意有护城河', f'{cur:.1f}%', rating,
                          f'近{len(rs)}年年报: ' + ', '.join(
                              f"{r['year']}:{r['roe']:.1f}%" for r in rs[-4:])))
        radar['盈利质量'] = min(100, int(cur / 25 * 100))
    else:
        factors.append(_f('roe', 'ROE 持续性', 'ROE Persistence', '巴菲特 · 质量/护城河',
                          '长期高 ROE ≈ 生意有护城河', '-', '数据不足', '缺年报权益/净利'))

    # ---- 2. 自由现金流/净利 (巴菲特·造血能力) ----
    opcf, _, _ = _latest_two_annual(cashflow_items, 'act_cash_flow_net')
    capex, _, _ = _latest_two_annual(cashflow_items, 'pay_fixed_assets_etc_cash')
    np_, _, _ = _latest_two_annual(income_items, 'parent_holder_net_profit')
    fcf_np = None
    if opcf is not None and capex is not None and np_:
        fcf = opcf - capex
        fcf_np = fcf / abs(np_) * 100 if np_ else 0.0
        rating = '积极' if fcf_np > 80 else ('中性' if fcf > 0 else '偏弱')
        factors.append(_f('fcf', '自由现金流/净利', 'FCF / Net Profit', '巴菲特 · 造血能力',
                          '利润要变成现金才算数', f'{fcf_np:.0f}%', rating,
                          f'经营CF {fmt_yi(opcf)} - CAPEX {fmt_yi(capex)} = {fmt_yi(fcf)}'))
    else:
        factors.append(_f('fcf', '自由现金流/净利', 'FCF / Net Profit', '巴菲特 · 造血能力',
                          '利润要变成现金才算数', '-', '数据不足', '缺现金流数据'))

    # ---- 3. 资产负债率 (格雷厄姆/巴菲特·安全) ----
    ann_b = _annual_items(balance_items)
    dr = None
    if ann_b:
        td, ta = ann_b[-1].get('total_debt'), ann_b[-1].get('assets_total')
        if td is not None and ta:
            dr = td / ta * 100
            rating = '积极' if dr < 50 else ('中性' if dr < 70 else '偏弱')
            factors.append(_f('debt', '资产负债率', 'Debt-to-Asset Ratio',
                              '格雷厄姆/巴菲特 · 安全', '负债是风险的放大器',
                              f'{dr:.1f}%', rating, '期末负债 / 期末总资产'))
            radar['安全'] = max(0, 100 - int(dr))
            if dr > 70:
                risk.append(f'资产负债率 {dr:.0f}% 偏高')
    else:
        factors.append(_f('debt', '资产负债率', 'Debt-to-Asset Ratio',
                          '格雷厄姆/巴菲特 · 安全', '负债是风险的放大器', '-', '数据不足', '缺资产负债表'))

    # ---- 4. PEG (林奇·成长×估值) ----
    np_cur, np_prev, _ = _latest_two_annual(income_items, 'parent_holder_net_profit')
    g = _pct(np_cur, np_prev)
    pe = (val or {}).get('pe_ttm')
    if pe and g is not None and g != 0:
        peg = abs(float(pe)) / abs(g)
        rating = '积极' if peg <= 1 else ('中性' if peg <= 2 else '偏弱')
        factors.append(_f('peg', 'PEG', 'PE ÷ Earnings Growth', '林奇 · 成长×估值',
                          'PEG≤1 成长才配得上估值', f'{peg:.1f}', rating,
                          f'PE(ttm) {pe} ÷ 净利增速 {g:.1f}%'))
        radar['成长'] = min(100, max(0, int(g)))
    else:
        factors.append(_f('peg', 'PEG', 'PE ÷ Earnings Growth', '林奇 · 成长×估值',
                          'PEG≤1 成长才配得上估值', '-', '数据不足',
                          '缺 PE 或净利同比' + ('(净利同比不可算)' if g is None else '')))
        radar['成长'] = min(100, max(0, int(g))) if g is not None else 0

    # ---- 5. 研发费用率 (费雪·成长质量) ----
    rd, _, _ = _latest_two_annual(income_items, 'research_and_development_expenses')
    rev, _, _ = _latest_two_annual(income_items, 'operating_income')
    rd_ratio = _div_pct(rd, rev)
    if rd_ratio is not None:
        rating = '积极' if rd_ratio >= 5 else ('中性' if rd_ratio >= 2 else '偏弱')
        factors.append(_f('rd', '研发费用率', 'R&D Ratio', '费雪 · 成长质量',
                          '研发投入是未来增长的燃料', f'{rd_ratio:.1f}%', rating,
                          '研发费用 / 营业收入'))
    else:
        factors.append(_f('rd', '研发费用率', 'R&D Ratio', '费雪 · 成长质量',
                          '研发投入是未来增长的燃料', '-', '数据不足', '缺研发费用'))

    # ---- 6. 毛利率水平与趋势 (费雪·盈利质量) ----
    rev_c, rev_p, _ = _latest_two_annual(income_items, 'operating_income')
    cost_c, cost_p, _ = _latest_two_annual(income_items, 'operating_costs')
    gm_cur = _div_pct(_sub(rev_c, cost_c), rev_c)
    gm_prev = _div_pct(_sub(rev_p, cost_p), rev_p)
    if gm_cur is not None:
        d = (gm_cur - gm_prev) if gm_prev is not None else 0.0
        rating = '积极' if (gm_cur >= 30 and d >= -1) else ('中性' if gm_cur >= 15 else '偏弱')
        trend = f'({d:+.1f}pp vs 去年)' if gm_prev is not None else ''
        factors.append(_f('gm', '毛利率水平与趋势', 'Gross Margin & Trend', '费雪 · 盈利质量',
                          '高且稳的毛利率 = 定价权', f'{gm_cur:.1f}%', rating, trend))
        q_scores = []
        if fcf_np is not None:
            q_scores.append(min(100, max(0, int(fcf_np / 1.5))))
        if rd_ratio is not None:
            q_scores.append(min(100, max(0, int(rd_ratio * 12))))
        q_scores.append(min(100, max(0, int(gm_cur * 3))))
        radar['质量'] = int(sum(q_scores) / len(q_scores)) if q_scores else 0
    else:
        factors.append(_f('gm', '毛利率水平与趋势', 'Gross Margin & Trend', '费雪 · 盈利质量',
                          '高且稳的毛利率 = 定价权', '-', '数据不足', '缺营收/成本'))

    # ---- 估值维度(价值) + 同业参照 ----
    if peer and peer.get('n') and peer.get('pe_median'):
        pe_med = peer['pe_median']
        radar['价值'] = (min(100, max(0, int(120 - (float(pe) / float(pe_med)) * 100)))
                         if pe and pe_med else 60)
    else:
        radar['价值'] = (60 if pe is None else
                         (100 if pe <= 15 else (70 if pe <= 25 else 40)))

    # ---- 风险扫描 (因子库§12) ----
    rev_c2, rev_p2, _ = _latest_two_annual(income_items, 'operating_income')
    g_rev = _pct(rev_c2, rev_p2)
    if g is not None and g < 0:
        risk.append(f'净利润同比下滑 {g:.1f}%')
    if opcf is not None and opcf < 0:
        risk.append('经营现金流为负')

    peer_out = {}
    if peer and peer.get('n'):
        pb_stock = (val or {}).get('pb_mrq')
        peer_out = {
            'n': peer['n'], 'name': peer.get('name', ''),
            'pe_med': peer.get('pe_median'), 'pb_med': peer.get('pb_median'),
            'pe_stock': pe, 'pb_stock': pb_stock,
            'pe_vs': ((float(pe) / float(peer['pe_median']) - 1) * 100
                      if pe and peer.get('pe_median') else None),
            'pb_vs': ((float(pb_stock) / float(peer['pb_median']) - 1) * 100
                      if pb_stock and peer.get('pb_median') else None),
        }

    return {'radar': radar, 'factors': factors,
            'matrix': {'rev': g_rev, 'np': g}, 'risk': risk, 'peer': peer_out}


# ========== 6. 三表明细(历史多期 + 同比 + 环比) ==========
FP_ORDER = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4, 'FY': 4, 'Annual': 4, 'Y': 4}
FP_NAME = {1: '一季', 2: '中报', 3: '三季', 4: '年报'}
_Q_TO_FP = {1: 'Q1', 2: 'Q2', 3: 'Q3', 4: 'Q4'}

STATEMENTS = {
    'income': {
        'path': '/api/a-share/financials/income-statements',
        'name': '利润表',
        'kind': 'flow',
        'fields': [
            ('operating_income', '营业收入', 'Operating Revenue', 'amount'),
            ('operating_costs', '营业成本', 'Operating Costs', 'amount'),
            ('gross_profit', '毛利', 'Gross Profit', 'amount'),
            ('gross_margin', '毛利率', 'Gross Margin', 'ratio'),
            ('sales_fee', '销售费用', 'Selling Expenses', 'amount'),
            ('manage_fee', '管理费用', 'G&A Expenses', 'amount'),
            ('research_and_development_expenses', '研发费用', 'R&D Expenses', 'amount'),
            ('rd_ratio', '研发费用率', 'R&D Ratio', 'ratio'),
            ('operating_profit', '营业利润', 'Operating Profit', 'amount'),
            ('profit_total', '利润总额', 'Total Profit', 'amount'),
            ('income_tax_expense', '所得税费用', 'Income Tax', 'amount'),
            ('net_profit', '净利润', 'Net Profit', 'amount'),
            ('parent_holder_net_profit', '归母净利润', 'Net Profit to Parent', 'amount'),
            ('basic_eps', '基本EPS', 'Basic EPS', 'num'),
        ],
    },
    'balance': {
        'path': '/api/a-share/financials/balance-sheets',
        'name': '资产负债表',
        'kind': 'point',
        'fields': [
            ('cash', '货币资金', 'Cash & Equivalents', 'amount'),
            ('accounts_receivable', '应收账款', 'Accounts Receivable', 'amount'),
            ('total_current_assets', '流动资产合计', 'Total Current Assets', 'amount'),
            ('non_current_nets_total', '非流动资产净值', 'Non-current Assets', 'amount'),
            ('assets_total', '资产总计', 'Total Assets', 'amount'),
            ('total_debt', '负债合计', 'Total Liabilities', 'amount'),
            ('holder_equity_total', '所有者权益合计', 'Total Equity', 'amount'),
            ('debt_ratio', '资产负债率', 'Debt-to-Asset Ratio', 'ratio'),
        ],
    },
    'cashflow': {
        'path': '/api/a-share/financials/cash-flow-statements',
        'name': '现金流量表',
        'kind': 'flow',
        'fields': [
            ('act_cash_flow_net', '经营现金流净额', 'Operating CF', 'amount'),
            ('invest_cash_flow_net', '投资现金流净额', 'Investing CF', 'amount'),
            ('financing_cash_flow_net', '筹资现金流净额', 'Financing CF', 'amount'),
            ('cash_equivalents_net_addition', '现金及等价物净增加', 'Net Cash Addition', 'amount'),
            ('pay_fixed_assets_etc_cash', '购建固定资产等支出', 'CAPEX', 'amount'),
            ('pay_dividends_profits_interest_cash', '分配股利/付息支出', 'Dividends & Interest', 'amount'),
        ],
    },
}


def _sub(a, b):
    if a is None or b is None:
        return None
    return a - b


def _div_pct(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b * 100


def _field_value(field, item):
    """取科目值, 含派生字段(毛利/毛利率/研发费用率/负债率)"""
    if item is None:
        return None
    if field == 'gross_profit':
        return _sub(item.get('operating_income'), item.get('operating_costs'))
    if field == 'gross_margin':
        return _div_pct(_sub(item.get('operating_income'), item.get('operating_costs')),
                        item.get('operating_income'))
    if field == 'rd_ratio':
        return _div_pct(item.get('research_and_development_expenses'),
                        item.get('operating_income'))
    if field == 'debt_ratio':
        return _div_pct(item.get('total_debt'), item.get('assets_total'))
    return item.get(field)


def build_single_quarter(items):
    """利润表/现金流(累计口径) → 单季值 {(year, fp): {field: v}}"""
    cmap = {}
    for it in items:
        y, fp = it.get('fiscal_year'), it.get('fiscal_period')
        if y is not None and fp:
            cmap[(y, fp)] = it
    out = {}
    for (y, fp), it in cmap.items():
        q = FP_ORDER.get(fp)
        if q and q > 1:
            prev = cmap.get((y, _Q_TO_FP[q - 1]))
            if prev is not None:
                s = {}
                for k, v in it.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        pv = prev.get(k)
                        s[k] = v - pv if isinstance(pv, (int, float)) else v
                    else:
                        s[k] = v
                out[(y, fp)] = s
                continue
        out[(y, fp)] = dict(it)      # Q1 或缺前一期 → 单季=累计
    return out


def single_quarter_series(items):
    """单季序列(按时间倒序), 自动丢弃缺前一期数据的最早一期(其"单季"实为累计值)
    返回 (keys, col_items, col_labels), 每列都是3个月可比
    """
    single = build_single_quarter(items)
    cmap = {(it.get('fiscal_year'), it.get('fiscal_period')): it for it in items
            if it.get('fiscal_year') is not None and it.get('fiscal_period')}
    keys = sorted(single.keys(), reverse=True)
    if keys:
        ly, lfp = keys[-1]
        lq = FP_ORDER.get(lfp)
        if lq and lq > 1 and (ly, _Q_TO_FP[lq - 1]) not in cmap:
            keys = keys[:-1]
    col_items = [single[k] for k in keys]
    col_labels = [f'{y}Q{FP_ORDER.get(fp)}' for (y, fp) in keys]
    return keys, col_items, col_labels


def _fmt_val(v, kind):
    if v is None:
        return '-'
    if kind == 'amount':
        return fmt_yi(v)
    return f'{v:.2f}'


def _fmt_cmp(curv, prevv, kind):
    """同比/环比格式化: amount → %, ratio → 百分点差, num → %"""
    if curv is None or prevv is None:
        return '-'
    if kind == 'ratio':
        return f'{curv - prevv:+.2f}'
    p = _pct(curv, prevv)
    return f'{p:+.1f}%' if p is not None else '-'


def render_statement(ths, st_key, limit=6, scope=None):
    """渲染单张表明细(同口径). scope: single(默认,flow单季)/h1(中报)/fy(年报)/all(期末快照)"""
    meta = STATEMENTS[st_key]
    j = ths_get(meta['path'], {'thscode': ths, 'period': 'quarterly', 'limit': limit})
    if not j or j.get('code') != 0:
        print(f'  {meta["name"]}: ' + ((j or {}).get('message') or '请求失败'))
        return
    items = (j.get('data') or {}).get('item', [])
    if not items:
        print(f'  {meta["name"]}: 无数据')
        return
    is_flow = meta['kind'] == 'flow'
    if scope is None:
        scope = 'single' if is_flow else 'all'
    cmap = {(it.get('fiscal_year'), it.get('fiscal_period')): it for it in items
            if it.get('fiscal_year') is not None and it.get('fiscal_period')}
    single = build_single_quarter(items) if is_flow else {}

    # --- 选列: 同口径 ---
    if is_flow and scope == 'single':
        keys, col_items, col_labels = single_quarter_series(items)
    else:
        target = {'h1': 2, 'fy': 4}.get(scope) if scope in ('h1', 'fy') else None
        keys = sorted((k for k in cmap if target is None or FP_ORDER.get(k[1]) == target),
                      reverse=True)
        col_items = [cmap[k] for k in keys]
        col_labels = [_period_label(y, fp) for (y, fp) in keys]
    if not col_items:
        print(f'  {meta["name"]}: 该口径暂无数据')
        return
    cur_key, cur = keys[0], col_items[0]

    def _yoy_item():
        if is_flow and scope == 'single':
            return single.get((cur_key[0] - 1, cur_key[1]))
        return cmap.get((cur_key[0] - 1, cur_key[1]))

    def _prev_item():
        # 环比: 单季看前一单季; 期末快照看上一期末; 同口径(h1/fy)无环比
        if (is_flow and scope == 'single') or ((not is_flow) and scope == 'all'):
            return col_items[1] if len(col_items) > 1 else None
        return None

    header = ['科目'] + col_labels + ['同比', '环比']
    rows = []
    for fname, label, _en, kind in meta['fields']:
        row = [label]
        for it in col_items:
            row.append(_fmt_val(_field_value(fname, it), kind))
        row.append(_fmt_cmp(_field_value(fname, cur), _field_value(fname, _yoy_item()), kind))
        pv = _prev_item()
        row.append(_fmt_cmp(_field_value(fname, cur), _field_value(fname, pv), kind) if pv else '-')
        rows.append(row)
    note = ('金额=亿元; 每列=单季(3个月), 环比=前一单季' if (is_flow and scope == 'single')
            else ('仅历年' + ('中报' if scope == 'h1' else '年报') + ', 同比=去年同期'
                  if scope in ('h1', 'fy') else '期末时点快照, 环比=上期期末'))
    print(f'\n[{st_key}] {meta["name"]}明细 ({note})')
    print(_tbl(header, rows))


# ========== 主流程 ==========
def show_overview(ths, ticker, name):
    """总览: 行情/估值/利润表速览/财务指标/研报"""
    print(f'\n{"=" * 70}')
    print(f'  {name} ({ths})  财报速览  {datetime.now():%Y-%m-%d %H:%M}')
    print(f'{"=" * 70}')

    # --- 行情快照 ---
    snap = get_snapshot(ths)
    if snap:
        rows = [
            ['最新价', fmt_num(snap.get('last_price')),
             '涨跌', f"{fmt_num(snap.get('price_change'), 2)} ({fmt_pct(snap.get('price_change_ratio_pct'))})"],
            ['今开', fmt_num(snap.get('open_price')),
             '最高/最低', f"{fmt_num(snap.get('high_price'))} / {fmt_num(snap.get('low_price'))}"],
            ['前收', fmt_num(snap.get('prev_price')),
             '成交额', fmt_yi(snap.get('turnover'))],
        ]
        print('\n[1] 行情快照')
        print(_tbl(['指标', '值', '指标', '值'], rows))

    # --- 估值 ---
    val = get_valuations(ths)
    if val:
        rows = [
            ['PE(TTM)', fmt_num(val.get('pe_ttm')), 'PE(MRQ)', fmt_num(val.get('pe_mrq'))],
            ['PB(MRQ)', fmt_num(val.get('pb_mrq')), 'PS(TTM)', fmt_num(val.get('ps_ttm'))],
            ['PCF(TTM)', fmt_num(val.get('pcf_ttm')), '', ''],
        ]
        print('\n[2] 估值')
        print(_tbl(['指标', '值', '指标', '值'], rows))

    # --- 利润表 ---
    rows, cur_key, prev_key, err = get_income_analysis(ths)
    if err:
        print(f'\n[3] 利润表速览: {err}')
    else:
        print('\n[3] 利润表速览(最近期及同比)')
        j = ths_get('/api/a-share/financials/income-statements',
                    {'thscode': ths, 'period': 'quarterly', 'limit': 4})
        raw = (j.get('data') or {}).get('item', []) if j and j.get('code') == 0 else []
        tbl_rows = []
        for it in raw:
            lbl = _period_label(it.get('fiscal_year'), it.get('fiscal_period'))
            tbl_rows.append([lbl,
                             fmt_yi(it.get('operating_income')),
                             fmt_yi(it.get('parent_holder_net_profit')),
                             fmt_num(it.get('basic_eps'))])
        print(_tbl(['报告期', '营业收入', '归母净利润', '基本EPS'], tbl_rows))
        if cur_key and prev_key:
            # 重取以拿 cur/prev 原始值
            jj = ths_get('/api/a-share/financials/income-statements',
                         {'thscode': ths, 'period': 'quarterly', 'limit': 10})
            allitems = (jj.get('data') or {}).get('item', []) if jj and jj.get('code') == 0 else []
            tmap = {(it.get('fiscal_year'), it.get('fiscal_period')): it for it in allitems}
            cur_it = tmap.get(cur_key)
            prev_it = tmap.get(prev_key)
            if cur_it and prev_it:
                rev_yoy = _pct(cur_it.get('operating_income'),
                               prev_it.get('operating_income'))
                np_yoy = _pct(cur_it.get('parent_holder_net_profit'),
                              prev_it.get('parent_holder_net_profit'))
                lbl = _period_label(cur_key[0], cur_key[1])
                print(f'  {lbl} 同比: 营收 {fmt_pct(rev_yoy)}, '
                      f'归母净利 {fmt_pct(np_yoy)}')

    # --- 财务指标 ---
    report, abilities, ierr = get_indicators_latest(ths)
    if abilities:
        print(f'\n[4] 财务指标(报告期 {report})')
        for cat, inds in abilities.items():
            line = '  '.join(f'{k} {fmt_num(v, 2)}' for k, v in inds.items())
            print(f'  {cat}: {line}')
    else:
        print(f'\n[4] 财务指标: {ierr}')

    # --- 研报 ---
    print('\n[5] 券商研报(东财, 最近8条)')
    reports = get_reports_eastmoney(ticker)
    if not reports:
        print('  (未获取到研报)')
    else:
        rrows = []
        for it in reports:
            title = it.get('title', '')
            title = title[:38] + '..' if len(title) > 40 else title
            rrows.append([title[:20] + ('..' if len(title) > 20 else ''),
                          it.get('orgSName', ''),
                          str(it.get('publishDate', ''))[:10],
                          it.get('emRatingName', '') or ''])
        print(_tbl(['标题', '机构', '日期', '评级'], rrows))

    print(f'\n{"=" * 70}')
    print(' 数据源: 同花顺 hithink-finance(财报/行情/估值) + 东财(研报)')
    print(' 以上仅供参考, 非投资建议')
    print(f'{"=" * 70}\n')


def main():
    parser = argparse.ArgumentParser(
        description='快速看财报: 总览 + 三表明细(同比/环比)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  python quick_report.py 600941              总览
  python quick_report.py 中国移动 -t income   利润表明细(历史+同比+环比)
  python quick_report.py 600941 -t balance   资产负债表
  python quick_report.py 600941 -t cashflow  现金流量表
  python quick_report.py 600941 -t all       总览 + 三表明细
  python quick_report.py 600941 -n 8         明细历史期数''')
    parser.add_argument('stock', help='股票代码(如600941)或名称(如中国移动)')
    parser.add_argument('-t', '--table', choices=['income', 'balance', 'cashflow', 'concept', 'all'],
                        default=None, help='深入查看的表(默认只看总览); concept=所属概念板块')
    parser.add_argument('-n', '--periods', type=int, default=6,
                        help='明细历史期数, 1-20 (默认6)')
    parser.add_argument('-b', '--board', default=None,
                        help='配合 -t concept 使用: 查看某板块成分股(代码, 如1215)')
    parser.add_argument('-s', '--scope', choices=['single', 'h1', 'fy', 'all'],
                        default=None,
                        help='报告口径: single(单季,3个月可比,利润/现金流默认)/h1(仅中报)/fy(仅年报)/all(期末快照,资产负债表默认)')
    args = parser.parse_args()

    if not API_KEY:
        print('错误: 未配置同花顺 API Key')
        print(f'  方式1: 设置环境变量 HITHINK_FINANCE_API_KEY')
        print(f'  方式2: 在本文件同目录创建 {CONFIG_PATH.name} 内含 {{"api_key": "..."}}')
        return

    ticker, ths, name, err = resolve_stock(args.stock)
    if err:
        print(err)
        return

    if args.table == 'all':
        show_overview(ths, ticker, name)
        for k in ('income', 'balance', 'cashflow'):
            render_statement(ths, k, args.periods, args.scope)
    elif args.table == 'concept':
        print(f'\n{"=" * 70}')
        print(f'  {name} ({ths})  所属概念板块  {datetime.now():%Y-%m-%d %H:%M}')
        print(f'{"=" * 70}')
        boards = get_concept_boards(ticker)
        if not boards:
            print('  (未获取到概念板块)')
        else:
            ordered = sorted(boards, key=lambda b: 0 if b.get('precise') == '1' else 1)
            print(_tbl(['板块代码', '板块名称', '类型'],
                       [[b['code'], b['name'], board_type(b)] for b in ordered]))
            if args.board:
                bc = str(args.board).upper()
                if bc.startswith('BK'):
                    bc = bc[2:]
                stocks = get_board_stocks(bc)
                if not stocks:
                    print(f'  (板块 {bc} 无成分股)')
                else:
                    print(f'\n  [BK{bc}] 成分股(按总市值降序, 前{min(len(stocks), 30)}条):')
                    srows = [[s['code'], s['name'],
                              fmt_num(s['price'], 2) if s['price'] is not None else '-',
                              fmt_pct(s['pct']) if s['pct'] is not None else '-',
                              fmt_yi(s['mcap']) if s['mcap'] else '-',
                              '◀本股' if s['code'] == ticker else '']
                             for s in stocks[:30]]
                    print(_tbl(['代码', '名称', '最新价', '涨跌幅', '总市值', ''], srows))
        print(f'{"=" * 70}\n')
    elif args.table:
        print(f'\n{"=" * 70}')
        print(f'  {name} ({ths})  {STATEMENTS[args.table]["name"]}明细  {datetime.now():%Y-%m-%d %H:%M}')
        print(f'{"=" * 70}')
        render_statement(ths, args.table, args.periods, args.scope)
        print('\n  (看总览: 去掉 -t 直接跑)')
        print(f'{"=" * 70}\n')
    else:
        show_overview(ths, ticker, name)
        print('  深入看某张表: -t income / -t balance / -t cashflow / -t all')


if __name__ == '__main__':
    main()
