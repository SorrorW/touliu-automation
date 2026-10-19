"""只读读取本次候选和执行台账，输出不含合作码的核验表。"""
from pathlib import Path
from collections import Counter
from datetime import datetime
import json,sys
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'投流'))
from 巨量千川.main_product_store import read_rows
BASE=ROOT/'docs/main-account-migration'

def main():
    preview=json.loads((BASE/'preview.json').read_text(encoding='utf-8'))
    sync={r['record_id']:r for r in read_rows('SELECT record_id,creator_id,product_id,plan_id,progress,synced,detail,updated_at FROM main_product_record_sync')}
    known={r['record_id'] for r in preview['rows']}
    verification_file=BASE/'sheet-verification.json'
    if verification_file.exists():
        verification=json.loads(verification_file.read_text(encoding='utf-8'))
        for record in verification.get('records',[]):
            if record['record_id'] not in known:
                preview['rows'].append(record)
    data=[]
    for item in preview['rows']:
        r=sync.get(item['record_id'])
        data.append({**{k:item.get(k,'') for k in ('table','record_id','creator','department','product','product_id','plan_id','binding')},
            'progress':r['progress'] if r else '尚未处理',
            'synced':bool(r and r['synced']),
            'detail':('平台拒绝：合作码错误，请业务员更新表格合作码后重试' if '合作码错误' in r['detail'] else r['detail']) if r else '本批次尚未处理到该记录',
            'verified_at':datetime.fromtimestamp(r['updated_at']).isoformat(timespec='seconds') if r else ''})
    counts=dict(Counter(r['progress'] for r in data))
    payload={'generated_at':datetime.now().isoformat(timespec='seconds'),'total':len(data),'counts':counts,'rows':data}
    (BASE/'execution-summary.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    def cell(v):return str(v).replace('|','/').replace('\n',' ')[:240]
    lines=['# 主号四主品本次处理结果','',f"核验时间：{payload['generated_at']}（运行机上海时间）。共 {len(data)} 条表格记录。",'']
    lines += [f'- {name}：{n}条' for name,n in counts.items()]
    lines += ['', '说明：“主号已加入/已拉取”包含既有关系复核；“等待达人接受”表示真实申请已提交，尚不能作为新增绑定成功。读取暂挂不会认定未绑定或业务失败。','',
        '|记录|抖音号|部门|主品|表格状态|表格读回|结果/原因|','|---|---|---|---|---|---|---|']
    for r in data:
        lines.append('|'+ '|'.join(cell(x) for x in (r['record_id'],r['creator'],r['department'],r['product'],r['progress'],'已一致' if r['synced'] else '未完成',r['detail']))+'|')
    (BASE/'execution-summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'total':len(data),'counts':counts,'synced':sum(r['synced'] for r in data)},ensure_ascii=False))
if __name__=='__main__':main()
