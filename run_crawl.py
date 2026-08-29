"""YTD TOP300 爬虫 — 全量版"""
import sys, time, sqlite3, requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

DB = Path(r'F:\QUANT\数据\代码\财报爬虫\finance.db')
START, END = '2025-12-31', '2026-06-30'
TOP_N = 300
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}

print('='*60)
print('  YTD TOP300 爬虫 (全A股)')
print('='*60)

# ── Step 1: 股票列表 ──
print('\n[1/3] 拉股票列表...', flush=True)
codes = set()
for p in range(1, 60):
    url = (f'https://push2delay.eastmoney.com/api/qt/clist/get?pn={p}&pz=500'
           '&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14')
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        items = r.json().get('data', {}).get('diff')
        if not items:
            break
        vals = items.values() if isinstance(items, dict) else items
        none_cnt = 0
        for item in vals:
            if item is None:
                none_cnt += 1; continue
            if not isinstance(item, dict):
                continue
            c = str(item.get('f12', ''))
            n = str(item.get('f14', ''))
            if len(c) == 6 and c.isdigit() and not c.startswith('8') and 'ST' not in n and '退' not in n:
                codes.add(c)
        ok = len(vals) - none_cnt
        print(f'  页{p}: {ok}只有效  (累计 {len(codes)})', flush=True)
        if none_cnt > len(vals) * 0.5:
            print(f'  限速, 停止翻页')
            break
    except Exception as e:
        print(f'  页{p} 异常: {e}')
        break
    time.sleep(0.5)

codes = sorted(codes)
print(f'  总计: {len(codes)} 只')

# ── Step 2: 拉K线 ──
print(f'\n[2/3] 拉日线算YTD ({len(codes)}只, 并发8)...')
results = {}
failed = 0
t0 = time.time()

def fetch(code):
    try:
        sym = f'{"sh" if code.startswith(("6","9")) else "sz"}{code}'
        url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
               f'CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=200')
        r = requests.get(url, timeout=8, headers=HEADERS)
        bars = r.json()
        if not bars or not isinstance(bars, list):
            return code, None
        # 找 YTD 起止日的 bar
        first_close = None
        last_close = None
        for b in bars:
            day = b.get('day', '')
            cls = float(b.get('close', 0))
            if day <= START and first_close is None:
                first_close = cls
            if day <= END:
                last_close = cls
            elif last_close is not None:
                break  # 超过 END 就停
        if first_close and last_close and first_close > 0:
            return code, round((last_close / first_close - 1) * 100, 2)
        return code, None
    except:
        return code, None

with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(fetch, c): c for c in codes}
    for i, f in enumerate(as_completed(futs)):
        code, r = f.result()
        if r is not None:
            results[code] = r
        else:
            failed += 1
        if (i+1) % 500 == 0:
            print(f'    [{i+1}/{len(codes)}] 有效={len(results)} 失败={failed} | {time.time()-t0:.0f}s', flush=True)

elapsed = time.time() - t0
print(f'  完成: {len(results)}有效 / {failed}失败, {elapsed:.1f}s')

sorted_r = sorted(results.items(), key=lambda x: x[1], reverse=True)
top = sorted_r[:TOP_N]
print(f'  TOP{TOP_N}: {top[0][1]:+.1f}% ~ {top[-1][1]:+.1f}%')
print(f'  TOP5: {[(c,r) for c,r in top[:5]]}')

# ── Step 3: 写库 ──
print(f'\n[3/3] 写入SQLite...')
conn = sqlite3.connect(str(DB))
cur = conn.cursor()
cur.execute('DELETE FROM quarterly')
for rank, (code, ytd) in enumerate(sorted_r, 1):
    cur.execute('INSERT OR REPLACE INTO quarterly (code,name,report_date,ytd_return,ytd_rank) VALUES (?,?,?,?,?)',
                (code, '', '2026-08-06', ytd, rank))
conn.commit()
cur.execute('SELECT COUNT(*) FROM quarterly')
print(f'  写入 {cur.fetchone()[0]} 条')
conn.close()
print('✅ 完成!')
