"""Render the colleague-facing business flow as PNG and editable SVG. No platform calls."""
from pathlib import Path
from html import escape
from math import atan2, sin, cos
import json
from PIL import Image, ImageDraw, ImageFont

OUT=Path(__file__).resolve().parent
W,H=1800,2780
BG='#f5f7fa'; INK='#142d44'; MUTED='#4a5f70'; LINE='#9babba'
BLUE='#235c88'; GREEN='#18745a'; AMBER='#99611a'; RED='#a34a42'
image=Image.new('RGB',(W,H),BG); draw=ImageDraw.Draw(image)
regular='C:/Windows/Fonts/msyh.ttc'; bold='C:/Windows/Fonts/msyhbd.ttc'
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',f'<rect width="{W}" height="{H}" fill="{BG}"/>']
checks=[]
def rect(x,y,w,h,fill,stroke=None,r=18):
 draw.rounded_rectangle((x,y,x+w,y+h),radius=r,fill=fill,outline=stroke,width=2)
 svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke or "none"}" stroke-width="2"/>')
def text(x,y,value,size=28,color=INK,weight=False,max_width=None):
 font=ImageFont.truetype(bold if weight else regular,size)
 width=draw.textlength(value,font=font)
 if max_width is not None:
  assert width<=max_width,(value,width,max_width)
 checks.append({'text':value,'x':x,'y':y,'width':round(width,1),'size':size})
 draw.text((x,y),value,font=font,fill=color,anchor='lt')
 svg.append(f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="{size}" font-weight="{600 if weight else 400}" fill="{color}">{escape(value)}</text>')
def arrow(x1,y1,x2,y2,color=LINE,width=4):
 draw.line((x1,y1,x2,y2),fill=color,width=width)
 a=atan2(y2-y1,x2-x1);n=13
 pts=[(x2,y2),(x2-n*cos(a-.52),y2-n*sin(a-.52)),(x2-n*cos(a+.52),y2-n*sin(a+.52))]
 draw.polygon(pts,fill=color)
 svg.append(f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{color}" stroke-width="{width}"/>')
 svg.append('<polygon points="'+' '.join(f'{x:.1f},{y:.1f}' for x,y in pts)+f'" fill="{color}"/>')
def node(n,y,h,title,lines):
 rect(80,y,980,h,'#ffffff','#d6e0e7')
 rect(104,y+25,59,59,BLUE,r=16)
 text(119 if n<10 else 111,y+33,str(n),34,'#ffffff',True)
 text(185,y+25,title,35,weight=True,max_width=840)
 for i,line in enumerate(lines):text(185,y+77+i*39,line,28,MUTED,max_width=835)
def branch(y,h,label,heading,lines,kind='wait'):
 colors={'wait':(AMBER,'#fff6e7'),'done':(GREEN,'#eaf6f0'),'block':(RED,'#fff0ed'),'sync':(BLUE,'#edf3fb')}
 color,fill=colors[kind]
 rect(1210,y,510,h,fill)
 text(1234,y+23,heading,31,color,True,max_width=460)
 for i,line in enumerate(lines):text(1234,y+66+i*33,line,25,INK,max_width=462)
 mid=y+h//2
 arrow(1060,mid,1205,mid,color)
 # A light label background keeps the branch label clear of its connector.
 label_font=ImageFont.truetype(regular,24);tw=draw.textlength(label,font=label_font)
 rect(1133-tw/2-9,mid-45,tw+18,34,BG,r=5)
 text(1133-tw/2,mid-45,label,24,color)

text(80,54,'四主品达人授权与加入计划',55,weight=True)
text(80,136,'同事沟通版  /  按商品匹配，统一主号处理',30,MUTED)
rect(80,202,1640,148,'#e8eff5')
text(108,225,'授权账号：鲜会鲜主号    ｜    类型：合作达人（含矩阵来源）',34,BLUE,True,max_width=1575)
text(108,284,'肥牛主品  ·  牛仔骨  ·  上脑  ·  牛板腱     业务部 / 业务员仅作归属统计',29,INK,max_width=1575)
text(85,379,'主流程',27,BLUE,True)
text(1210,379,'分支结果与后续处理',27,MUTED,True)

nodes=[
 (1,428,150,'业务同事填表', ['主表 / 旧素材表均可；填写达人账号、合作码、', '商品ID、实际投放日期，并维护可处理状态。']),
 (2,638,150,'检查商品、资料与人工停投要求', ['按商品ID匹配四主品；核对达人资料和停投备注。', '跨表同达人＋同主品只做一次，结果分别回写。']),
 (3,848,170,'完整查询对应主品计划：已有有效绑定？', ['先核对主号、商品与计划，再读取完整达人名单。', '读取失败 / 分页不完整 → 暂挂复查，不当成未绑定。']),
 (4,1078,150,'未绑定：先检查佣金限制', ['当前上限5%；等于5%可以继续，超过上限暂不处理。', '历史“已删除”关系按未绑定重新核验。']),
 (5,1288,170,'主号检查并补齐两项授权', ['① 抖音号授权    ② 商品投放授权', '已生效或已申请待确认的权限，均不重复提交。']),
 (6,1518,150,'两项授权生效后：是否已到投放日期？', ['可以提前申请授权。', '到达预约投放日期后，才继续加入对应主品计划。']),
 (7,1728,150,'提交前再次查绑定：平台是否已自动拉取？', ['只检查当前达人对应的这个主品计划。', '同一达人涉及多个主品时，各主品分别核验。']),
 (8,1938,170,'添加到现有主品计划，并重新完整复核', ['仅向对应计划添加达人，确认存在有效绑定。', '不新建分部计划，不修改主品ROI、预算或启停。']),
 (9,2168,170,'同步结果，读回确认后闭环', ['同步表格进度、主号绑定快照及动作台账。', '成功以平台有效绑定＋数据库与表格读回一致为准。']),
]
for n,y,h,title,lines in nodes:node(n,y,h,title,lines)
labels=['进入候选','符合处理条件','未绑定 / 已删除','未超佣金上限','两项均已生效','已到投放日期','仍无有效绑定','已确认加入成功']
for i in range(len(nodes)-1):
 _,y,h,*_=nodes[i];ny=nodes[i+1][1];arrow(570,y+h+2,570,ny-7)
 rect(591,y+h+9,300,33,BG,r=4);text(599,y+h+7,labels[i],24,MUTED)
branch(638,150,'不符合','转人工或保留原状态', ['非四主品 / 资料异常 / 停投', '→ 待人工处理；历史已完成跳过。'],'block')
branch(848,170,'已有绑定','直接转第9步同步', ['自动拉取 → 主号已拉取', '已有手动绑定 → 主号已加入', '无需重新申请或重复添加。'],'done')
branch(1078,150,'超上限','佣金过高', ['先确认佣金条件，调整完成后', '再按流程复查。'],'block')
branch(1288,170,'待确认','等待达人接受', ['业务同事提醒达人确认。', '系统保存进度并释放浏览器，', '后续轮询继续核验，不记失败。'])
branch(1518,150,'未到日期','待加入主品', ['保留已完成授权，等待预约日，', '之后重新核验绑定再继续。'])
branch(1728,150,'已有绑定','直接转第9步同步', ['沿用实际绑定来源回写结果，', '跳过添加操作。'],'done')
branch(1938,170,'未能确认','保留原因，先核查再重试', ['读取异常 → 待加入主品', '平台明确拦截 → 待人工处理', '先查是否已成功，防止重复添加。'])
branch(2168,170,'回写失败','仅补同步，不重复平台操作', ['保留平台成功结果和同步断点，', '下次只补表格回写并读回确认。'],'sync')
rect(80,2420,1640,264,'#e8eff5')
text(108,2446,'同事需要记住的三件事',34,BLUE,True)
text(108,2510,'1. 填对商品ID；一个达人多个主品，要分别看对应计划的结果。',30,INK,max_width=1570)
text(108,2562,'2. “等待达人接受 / 待加入主品”是中间状态，不等于投放失败或已成功加入。',30,INK,max_width=1570)
text(108,2614,'3. 桌面与自动化需保持开启；系统随轮询接续处理，异常记录保留具体原因。',30,INK,max_width=1570)
text(80,2716,'范围：表格达人授权与加入主品计划。原分部计划的ROI、预算、日报和收口沿原规则执行。',26,MUTED,max_width=1640)
svg.append('</svg>')
image.save(OUT/'main-account-flow.png',optimize=True)
(OUT/'main-account-flow.svg').write_text('\n'.join(svg),encoding='utf-8')
image.save(OUT/'main-account-flow.pdf','PDF',resolution=150.0)
print(json.dumps({'image_size':[W,H],'validated_text_lines':len(checks),'outputs':['main-account-flow.png','main-account-flow.svg','main-account-flow.pdf']},ensure_ascii=False))
