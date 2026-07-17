#!/usr/bin/env python3
import sys, yaml, requests, json, os
cfg = yaml.load(open(os.path.expanduser("~/quantpilot/config.yaml")), Loader=yaml.FullLoader)
wc = cfg["wechat"]
token = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken", params={"corpid": wc["corp_id"], "corpsecret": wc["agent_secret"]}, timeout=10).json()["access_token"]
payload = {"touser":"YangJie","msgtype":"text","agentid":wc["agent_id"],"text":{"content":open("/tmp/wechat_msg.json").read().strip()}}
requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}", json=payload, timeout=10)
print("pushed")