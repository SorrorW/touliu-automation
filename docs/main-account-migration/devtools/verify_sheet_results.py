"""只读核验本轮表格与主号台账，并保存不含凭据的归属信息。"""
from pathlib import Path
import json,sys
from datetime import datetime
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'wecom-agent'),str(ROOT/'投流')]
from main_account_flow import load_sheets
from wecom_client import WeComClient
from 巨量千川.main_product_store import read_rows
from 巨量千川.main_products import product_route
BASE=ROOT/'docs/main-account-migration'
client=WeComClient();sheets=load_sheets(client.cfg)
records={}
for item in sheets:
    for rid,r in item['simple'].items():
        route=product_route(r.get('商品ID'))
        records[rid]={'record_id':rid,'table':item['name'],'creator':r.get('抖音号',''),
            'department':r.get('业务部',''),'product':route.name if route else '',
            'product_id':route.product_id if route else '', 'plan_id':route.plan_id if route else '',
            'progress':r.get('进度','')}
sync=read_rows('SELECT record_id,progress,synced FROM main_product_record_sync')
checks=[{'record_id':r['record_id'],'expected':r['progress'],
        'actual':records.get(r['record_id'],{}).get('progress'),
        'matches':r['progress']==records.get(r['record_id'],{}).get('progress'),'synced':bool(r['synced'])} for r in sync]
payload={'generated_at':datetime.now().isoformat(timespec='seconds'),
    'checked':len(checks),'matched':sum(r['matches'] for r in checks),
    'checks':checks,'records':[records[r['record_id']] for r in sync if r['record_id'] in records]}
(BASE/'sheet-verification.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in payload.items() if k not in ('checks','records')},ensure_ascii=False))
for r in checks:
    if not r['matches']:print(json.dumps(r,ensure_ascii=False))
