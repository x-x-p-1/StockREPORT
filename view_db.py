import sqlite3
conn = sqlite3.connect(r'F:\QUANT\数据\代码\财报爬虫\finance.db')
cur = conn.cursor()

print('=' * 65)
print('  TOP300 YTD 赢家数据库  (finance.db -> quarterly)')
print('=' * 65)

cur.execute('SELECT COUNT(*), MIN(ytd_return), MAX(ytd_return), AVG(ytd_return) FROM quarterly')
cnt, lo, hi, avg = cur.fetchone()
print(f'  总记录: {cnt}  |  最低: {lo:+.1f}%  |  最高: {hi:+.1f}%  |  均值: {avg:+.1f}%')

cur.execute('''SELECT 
    SUM(CASE WHEN ytd_return >= 100 THEN 1 ELSE 0 END),
    SUM(CASE WHEN ytd_return >= 50 AND ytd_return < 100 THEN 1 ELSE 0 END),
    SUM(CASE WHEN ytd_return >= 20 AND ytd_return < 50 THEN 1 ELSE 0 END),
    SUM(CASE WHEN ytd_return >= 0 AND ytd_return < 20 THEN 1 ELSE 0 END),
    SUM(CASE WHEN ytd_return < 0 THEN 1 ELSE 0 END)
FROM quarterly''')
a,b,c,d,e = cur.fetchone()
print(f'  分布: 翻倍({a}) | 50~100%({b}) | 20~50%({c}) | 0~20%({d}) | 下跌({e})')

print()
print(f'  {"排名":<5} {"代码":<10} {"YTD":>8}')
print(f'  {"-"*25}')

cur.execute('SELECT code, ytd_return, ytd_rank FROM quarterly ORDER BY ytd_rank LIMIT 30')
for code, ytd, rank in cur.fetchall():
    bar = '|' * min(int(abs(ytd) / 25), 30)
    print(f'  {rank:<5} {code:<10} {ytd:>+7.1f}%  {bar}')

print(f'  ...  (中间 {cnt-40} 条省略)  ...')

cur.execute('SELECT code, ytd_return, ytd_rank FROM quarterly ORDER BY ytd_rank DESC LIMIT 10')
for code, ytd, rank in reversed(cur.fetchall()):
    print(f'  {rank:<5} {code:<10} {ytd:>+7.1f}%')

conn.close()
