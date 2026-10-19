"""按指定记录补查，保留主号校验与互斥；不扩大到历史记录。"""
from pathlib import Path
import argparse, json, sys
from datetime import datetime
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'wecom-agent'),str(ROOT/'投流')]
from main_account_flow import Batch, browser_guard, WORKFLOW_VERSION
from wecom_client import WeComClient
import poll_and_run as poll

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--record',action='append',required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    client=WeComClient()
    counts=[]
    with browser_guard():
        batch=Batch(client.cfg)
        state=poll.load_state()
        for rid in dict.fromkeys(args.record):
            if poll.report_busy() or poll.plan_power_busy() or poll.near_report_slot():
                break
            item=next((s for s in batch.sheets if rid in s['simple']),None)
            if not item:
                counts.append({'record_id':rid,'reason':'record not found'})
                continue
            opts=SimpleNamespace(retry=rid,force=False,dry_run=False,main_limit=1)
            counts.append({'record_id':rid,'counts':batch.process_sheet(client,item,state,opts,poll)})
        poll.write_status(state='idle',sheet='',record_id='',step='',note='主号指定记录补查完成')
        payload={'version':WORKFLOW_VERSION,'generated_at':datetime.now().isoformat(timespec='seconds'),
                 'counts':counts,'results':[{'account_id':k[0],'creator_id':k[1],
                     'product_id':k[2],'plan_id':k[3],**v} for k,v in batch.workflow.results.items()]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    return 0
if __name__=='__main__':raise SystemExit(main())
