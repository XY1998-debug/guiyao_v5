import os, polars as pl, yaml, requests, json, sys
sys.path.insert(0, '/home/ubuntu/quantpilot')
cfg = yaml.load(open('/home/ubuntu/quantpilot/config.yaml'), Loader=yaml.FullLoader)
wc = cfg['wechat']
def token():
    return requests.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
        params={'corpid':wc['corp_id'],'corpsecret':wc['agent_secret']},timeout=10).json()['access_token']
def push(text):
    t = token()
    requests.post(f'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={t}',
        json={'touser':'YangJie','msgtype':'text','agentid':wc['agent_id'],'text':{'content':text}},timeout=10)
ALERTS = '/home/ubuntu/guiyao_v5/data/price_alerts.parquet'
LAST = 0.0
def check():
    global LAST
    if not os.path.exists(ALERTS): return
    mt = os.path.getmtime(ALERTS)
    if mt <= LAST: return
    LAST = mt
    df = pl.read_parquet(ALERTS)
    for r in df.iter_rows(named=True):
        text = '[归爻价格提醒] ' + r.get('name','') + '(' + str(r.get('code','')) + ')'
        if r.get('direction')=='buy' and not r.get('triggered_buy'):
            text += ' 建议买入价 ' + str(r.get('entry_price',0)) + ' 止损 ' + str(r.get('stop_loss',0))
            push(text)
    print('price_checked:', len(df))
if __name__ == '__main__':
    check()
