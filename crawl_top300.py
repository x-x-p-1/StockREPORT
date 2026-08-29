# 财报爬虫主脚本 (Eastmoney股票列表 + 腾讯K线并发)
# 流程: 全A股列表 → 腾讯K线并发 → 算YTD → TOP300 → SQLite

import sys, os, time, sqlite3
import requests
from pathlib import Path
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ========== 配置 ==========
DB_PATH = Path(r'F:\QUANT\数据\代码\财报爬虫\finance.db')
YTD_START = '2025-12-31'
YTD_END   = '2026-06-30'
TOP_N     = 300

# ========== 1. 获取全A股列表 (Eastmoney HTTP API) ==========
def get_all_stocks():
    """东方财富行情拉全部A股(过滤ST/退市/北交所)"""
    codes = set()
    for page in range(1, 60):
        url = ('https://push2delay.eastmoney.com/api/qt/clist/get'
               f'?pn={page}&pz=500'
               '&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
               '&fields=f12,f14')
        try:
            r = requests.get(url, timeout=15,
                headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
            data = r.json()
            items = data.get('data', {}).get('diff')
            if not items:
                break
            # items 可能是 dict (正常) 或 list
            vals = items.values() if isinstance(items, dict) else items
            empty_count = 0
            for item in vals:
                if item is None:
                    empty_count += 1
                    continue  # 跳过 None 值(限速/异常)
                if not isinstance(item, dict):
                    continue
                code = str(item.get('f12', ''))
                name = str(item.get('f14', ''))
                if (len(code) == 6 and code.isdigit() and not code.startswith('8')
                    and 'ST' not in name and '退' not in name):
                    codes.add(code)
            # 如果大部分都是 None, 说明被限速了
            if empty_count > len(vals) * 0.5:
                print(f'  页{page}: {empty_count}/{len(vals)} 空值(疑似限速), 停止翻页')
                break
        except Exception as e:
            print(f'  页{page} 失败: {e}')
            break
        time.sleep(0.5)
    codes = sorted(codes)
    print(f'  沪深A股(不含ST/退市/北交所): {len(codes)} 只')
    return codes

# ========== 2. 腾讯K线并发拉取 + 算 YTD ==========
def fetch_ytd_returns(codes):
    """腾讯K线HTTP API并发拉取(前复权qfq), 返回 [(code, ytd), ...]"""

    results = {}
    failed = 0
    total = len(codes)

    def _fetch_one(code):
        try:
            sym = f'{"sh" if code.startswith(("6","9")) else "sz"}{code}'
            url = ('http://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
                   f'?param={sym},day,{YTD_START},{YTD_END},200,qfq')
            r = requests.get(url, timeout=10,
                headers={'User-Agent':'Mozilla/5.0'})
            d = r.json()
            klines = d.get('data', {}).get(sym, {}).get('qfqday')
            if not klines:
                # 没复权数据, 尝试不复权
                klines = d.get('data', {}).get(sym, {}).get('day')
            if not klines or len(klines) < 1:
                return code, None
            # kline: [date, open, close, high, low, volume]
            p_start = float(klines[0][1])   # 第一条开盘价
            p_end   = float(klines[-1][2])  # 最后一条收盘价
            if p_start <= 0:
                return code, None
            return code, round((p_end / p_start - 1) * 100, 2)
        except Exception:
            return code, None

    print(f'  拉日线: 共 {total} 只, 并发=16 ...')
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_fetch_one, c): c for c in codes}
        for i, f in enumerate(as_completed(futs)):
            code, r = f.result()
            if r is not None:
                results[code] = r
            else:
                failed += 1
            if (i + 1) % 200 == 0:
                elapsed = time.time() - t0
                eta = elapsed / (i + 1) * (total - i - 1)
                print(f'    [{i+1}/{total}] 有效={len(results)} 失败={failed} | {elapsed:.0f}s ETA {eta:.0f}s')

    elapsed = time.time() - t0
    print(f'  完成: {len(results)} 有效 / {failed} 失败, 耗时 {elapsed:.1f}s')

    return sorted(results.items(), key=lambda x: x[1], reverse=True)

# ========== 3. SQLite 写入 ==========
def save_to_db(rows, ytd_results):
    """rows: [(code, ytd_return, name), ...]; ytd_results: list[(code, ytd)]"""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute('DELETE FROM quarterly')
    conn.commit()

    for rank, (code, ytd) in enumerate(ytd_results, 1):
        try:
            cur.execute(
                'INSERT OR REPLACE INTO quarterly (code, name, report_date, ytd_return, ytd_rank) '
                'VALUES (?,?,?,?,?)',
                (code, '', str(date.today()), ytd, rank)
            )
        except Exception:
            pass

    conn.commit()
    # 统计
    cur.execute('SELECT COUNT(*) FROM quarterly')
    cnt = cur.fetchone()[0]
    conn.close()
    print(f'  写入 {cnt} 条记录到 quarterly 表')

# ========== 主流程 ==========
def main():
    print('=' * 60)
    print('  财报爬虫 — TOP300 YTD 赢家 (Eastmoney+腾讯K线)')
    print('=' * 60)

    # Step 1: 全 A 股列表
    print('\n[1/3] 获取全 A 股列表...')
    codes = get_all_stocks()
    if not codes:
        print('❌ 未能获取股票列表')
        return

    # Step 2: 拉日线 + 算 YTD
    print(f'\n[2/3] 拉日线计算 YTD 涨跌幅 (基准: {YTD_START}, {len(codes)} 只)...')
    ytd_results = fetch_ytd_returns(codes)

    if not ytd_results:
        print('❌ 无有效 YTD 数据')
        return

    top = ytd_results[:TOP_N]
    print(f'  TOP{TOP_N} 范围: {top[0][1]:+.1f}% ~ {top[-1][1]:+.1f}%')
    print(f'  TOP5: {[(c,r) for c,r in top[:5]]}')

    # Step 3: 写入 SQLite
    print(f'\n[3/3] 写入 SQLite...')
    save_to_db(top, ytd_results)
    print('✅ 全部完成! (财报字段下一步填充)')

if __name__ == '__main__':
    main()
