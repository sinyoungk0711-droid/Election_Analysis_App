"""Explicit initialization and local demo setup. No default production credentials."""
import argparse
import csv
import io
import json
from pathlib import Path

from .db import T, DATA, now, create_engine, initialize, rows, one, add, update
from .service import issue_token, save_blob, enqueue, approved, enforce_ready
from .worker import METRICS, YEARS, tick

QUESTIONS=[
 '2016년 전국 최종 투표율','2020년 전국 최종 투표율','2024년 전국 최종 투표율',
 '2016년 대비 2020년 전국 투표율 증감','2020년 대비 2024년 전국 투표율 증감',
 '2016년 최고 투표율 지역','2020년 최고 투표율 지역','2024년 최고 투표율 지역',
 '2016년 최저 투표율 지역','2020년 최저 투표율 지역','2024년 최저 투표율 지역',
 '2016년 최고·최저 투표율 차이','2020년 최고·최저 투표율 차이','2024년 최고·최저 투표율 차이',
 '2016년 지역 투표율 표준편차','2020년 지역 투표율 표준편차','2024년 지역 투표율 표준편차',
 '세 선거 모두 전국보다 낮은 지역 수','2020년 대비 2024년 하락 지역 수','2016년과 2024년 지역 격차 비교']

def init_reference(c):
    if not rows(c,'election'):
        from datetime import date
        for n,y,day in [(20,2016,'2016-04-13'),(21,2020,'2020-04-15'),(22,2024,'2024-04-10')]:
            add(c,'election',election_no=n,election_year=y,election_date=date.fromisoformat(day),title=f'제{n}대 국회의원선거')
    if not rows(c,'metric_definition'):
        for code,unit,formula in METRICS:add(c,'metric_definition',metric_code=code,definition_version=1,unit=unit,formula=formula,null_policy='결측은 0으로 대체하지 않으며 공통 비교에서 제외')
    if not rows(c,'fact_check'):
        for n,q in enumerate(QUESTIONS,1):add(c,'fact_check',fc_code=f'FC{n:02}',question=q,replacement_note=None)

def create_user(c,name,roles):
    if rows(c,'app_user',login_subject=name):raise ValueError('이미 존재하는 사용자입니다.')
    uid=add(c,'app_user',login_subject=name,display_name=name,is_active=True,created_at=now())
    for role in roles:add(c,'user_role',user_id=uid,role_code=role,granted_by=uid,granted_at=now())
    return uid,issue_token(c,uid)

def demo_csv():
    buffer=io.StringIO();w=csv.writer(buffer)
    w.writerow(['election_year','province','region_code','region_name','eligible_voters','voters','official_turnout'])
    for year,counts in [(2016,[60000,55000,65000]),(2020,[68000,60000,70000]),(2024,[70000,59000,72000])]:
        for index,count in enumerate(counts,1):w.writerow([year,'가상도',f'DEMO-{index}',f'가상{index}시',100000,count,f'{count/1000:.1f}'])
        w.writerow([year,'','NATIONAL','가상 전국',300000,sum(counts),f'{sum(counts)/3000:.1f}'])
    return buffer.getvalue().encode('utf-8-sig')

def pdf_bytes(label):
    stream=f'BT /F1 18 Tf 40 780 Td (SYNTHETIC DEMO - {label}) Tj ET'.encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
     b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
     b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    output=b'%PDF-1.4\n';offsets=[]
    for i,obj in enumerate(objects,1):offsets.append(len(output));output+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    xref=len(output);output+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{o:010} 00000 n \n'.encode() for o in offsets)
    return output+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF'

def seed_demo(engine):
    with engine.begin() as c:
        init_reference(c)
        if rows(c,'dataset_version'):raise ValueError('데이터가 있는 DB에는 데모를 추가하지 않습니다. APP_DATA_DIR로 새 경로를 사용하세요.')
        accounts=[]
        for name,roles in [('데모 작성자',['ANALYST','SEARCHER','EDITOR']),('데모 검증자',['REVIEWER']),('데모 총괄',['LEAD'])]:
            uid,token=create_user(c,name,roles);accounts.append({'user_id':str(uid),'name':name,'roles':roles,'token':token})
        writer,reviewer,lead=[int(x['user_id']) for x in accounts]
        source=add(c,'source_document',title='가상 예제 CSV — 실제 선거 결과 아님',publisher='데모 생성기',published_on=None,source_url=None,usage_terms='SYNTHETIC DEMO ONLY')
        uri,digest=save_blob(demo_csv());fid=add(c,'source_file',source_id=source,storage_uri=uri,sha256=digest,collected_at=now(),collection_scope='세 가상 지역과 가상 전국 합계')
        did=add(c,'dataset_version',version_no=1,status='DRAFT',created_by=writer,created_at=now(),frozen_at=None)
        enqueue(c,{'user_id':writer},'IMPORT',{'target_table':'dataset_version','target_id':did,'token':1,'file_ids':[fid]})
    tick(engine)
    with engine.begin() as c:
        import_job=rows(c,'api_job')[0]
        if import_job['status']!='SUCCEEDED':raise ValueError(import_job['error'])
        ds=one(c,'dataset_version',did)
        enqueue(c,{'user_id':writer},'QUALITY_CHECK',{'target_table':'dataset_version','target_id':did,'token':ds['revision_token'],'rule_version':'quality-v1'})
    tick(engine)
    def approve(c,kind,pk,who):
        from .service import TARGETS,target,review_auth
        r=review_auth(c,{'user_id':who,'roles':['REVIEWER','LEAD']},kind,pk);enforce_ready(c,kind,pk)
        fields={v[1]:None for v in TARGETS.values()};fields[TARGETS[kind][1]]=pk
        add(c,'approval',**fields,reviewer_id=who,decision='APPROVED',review_note='명시적으로 생성한 가상 시연 자료의 독립 역할 검토',decided_at=now(),snapshot_token=r.get('revision_token',1))
    with engine.begin() as c:
        approve(c,'DATASET',did,reviewer);update(c,'dataset_version',did,status='FROZEN',frozen_at=now())
        run=add(c,'analysis_run',dataset_id=did,code_ref='builtin-v1',parameters={'years':list(YEARS),'baseline':'NATIONAL_OFFICIAL','comparison_basis':'COMMON_REGIONS'},executed_by=writer,executed_at=now(),status='RUNNING')
        enqueue(c,{'user_id':writer},'ANALYSIS',{'run_id':run})
    tick(engine)
    with engine.begin() as c:
        approve(c,'ANALYSIS_RUN',run,reviewer)
        results=rows(c,'analysis_result',run_id=run);metrics={m['metric_id']:m['metric_code'] for m in rows(c,'metric_definition')}
        def find(code,year=None,regional=False,years=None):
            return [r for r in results if metrics[r['metric_id']]==code and (r['region_id'] is not None)==regional and (year is None or r['context']['years']==[year]) and (years is None or r['context']['years']==years)]
        selected=[find('TURNOUT',y)[0] for y in YEARS]+[find('CHANGE',years=[2016,2020])[0],find('CHANGE',years=[2020,2024])[0]]
        selected+=[max(find('TURNOUT',y,True),key=lambda r:r['value']) for y in YEARS]
        selected+=[min(find('TURNOUT',y,True),key=lambda r:r['value']) for y in YEARS]
        selected+=[find('RANGE',y)[0] for y in YEARS]+[find('STDDEV',y)[0] for y in YEARS]
        selected+=[find('REPEATED_LOW',years=list(YEARS))[0],find('DECLINED',years=[2020,2024])[0],find('STDDEV',2024)[0]]
        article=add(c,'article',slug='synthetic-demo-investigation',created_by=writer,created_at=now())
        blocks=[{'block_id':'intro','type':'HEADING','text':'가상 데이터 시연 — 실제 총선 결과가 아닙니다'}];claimtexts=[]
        for n,r in enumerate(selected,1):
            unit=one(c,'metric_definition',r['metric_id'])['unit'];suffix={'PERCENT':'%','PP':'%p','COUNT':'곳','RANK':'위'}[unit]
            label=one(c,'region',r['region_id'])['name'] if r['region_id'] else '가상 전체 기준'
            text=f"{QUESTIONS[n-1]} — {label}: {r['value']:.2f}{suffix}"
            if n==20:
                earlier=find('STDDEV',2016)[0];text=f"가상 지역 격차 표준편차는 2016년 {earlier['value']:.2f}%p, 2024년 {r['value']:.2f}%p다. 다른 격차 지표도 함께 검토해야 한다."
            blocks.append({'block_id':f'p{n}','type':'PARAGRAPH','text':text});claimtexts.append(text)
        rid=add(c,'article_revision',article_id=article,version_no=1,title='세 번의 총선, 참여 격차를 추적하다 — 가상 예제',body=json.dumps({'blocks':blocks},ensure_ascii=False),created_by=writer,created_at=now(),change_note='가상 데이터 시연용 최초본')
        for n,(r,text) in enumerate(zip(selected,claimtexts),1):
            cid=add(c,'claim',revision_id=rid,claim_no=n,text=text,body_locator=f'p{n}:0:{len(text)}',is_core=True,is_numeric=True)
            add(c,'claim_evidence',claim_id=cid,result_id=r['result_id'],evidence_id=None,relation_type='SUPPORT',note='가상 데이터 계산 결과')
            if n==20:add(c,'claim_evidence',claim_id=cid,result_id=find('STDDEV',2016)[0]['result_id'],evidence_id=None,relation_type='SUPPORT',note='가상 데이터 기준연도 비교')
            fc=rows(c,'fact_check',fc_code=f'FC{n:02}')[0]
            fr=add(c,'fact_check_revision',fc_id=fc['fc_id'],version_no=1,claim_id=cid,method='가상 원본·분석 행 재계산',finding='가상 예제 값 일치. 실제 선거 사실을 입증하지 않음.',verdict='SUPPORTED',checked_by=reviewer,checked_at=now())
            approve(c,'FACT_CHECK_REVISION',fr,lead)
        enqueue(c,{'user_id':writer},'RULE_CHECK',{'target_table':'article_revision','target_id':rid,'rule_version':'rules-v1'})
        chart=add(c,'chart',chart_key='demo-turnout',version_no=1,title='가상 지역의 3회 투표율',chart_spec={'type':'line','unit':'PERCENT','synthetic':True},created_by=writer)
        for order,r in enumerate([r for r in results if metrics[r['metric_id']]=='TURNOUT']):add(c,'chart_result',chart_id=chart,result_id=r['result_id'],display_order=order)
    tick(engine)
    with engine.begin() as c:
        approve(c,'ARTICLE_REVISION',rid,lead)
        pub=add(c,'publication',version_no=1,status='DRAFT',created_by=writer,created_at=now(),published_at=None,correction_note='가상 예제 데이터로 구성된 시연 공개본',report_uri=None,journal_uri=None)
        add(c,'publication_item',publication_id=pub,revision_id=rid,chart_id=None,display_order=0)
        add(c,'publication_item',publication_id=pub,revision_id=None,chart_id=chart,display_order=1)
        fileids=[]
        for label in ['Analysis report','News journal']:
            uri,digest=save_blob(pdf_bytes(label));fileids.append(add(c,'handover_file',publication_id=pub,category='FINAL',storage_uri=uri,sha256=digest,access_scope='PUBLIC',verified_at=now()))
        update(c,'publication',pub,report_file_id=fileids[0],journal_file_id=fileids[1],report_uri=one(c,'handover_file',fileids[0])['storage_uri'],journal_uri=one(c,'handover_file',fileids[1])['storage_uri'])
        enforce_ready(c,'PUBLICATION',pub);update(c,'publication',pub,status='FROZEN');approve(c,'PUBLICATION',pub,lead)
        update(c,'publication',pub,status='PUBLISHED',published_at=now())
    (DATA/'DEMO').write_text('SYNTHETIC DATA ONLY',encoding='utf-8')
    (DATA/'demo-accounts.json').write_text(json.dumps(accounts,ensure_ascii=False),encoding='utf-8')
    return accounts

def main():
    parser=argparse.ArgumentParser(description='선거 탐사보도 초기화')
    parser.add_argument('command',choices=['init','demo','create-user','issue-token','schema'])
    parser.add_argument('--name');parser.add_argument('--roles',default='ANALYST,EDITOR')
    args=parser.parse_args();engine=create_engine();initialize(engine)
    if args.command=='demo':
        seed_demo(engine);print('가상 예제 생성 완료. APP_DEMO_MODE=1은 로컬 시연 전용입니다.')
    elif args.command=='schema':
        from .db import metadata
        from sqlalchemy.schema import CreateTable
        print('\n\n'.join(str(CreateTable(t).compile(engine))+';' for t in metadata.sorted_tables))
    else:
        with engine.begin() as c:
            init_reference(c)
            if args.command=='create-user':
                roles=args.roles.split(',')
                if not args.name or not set(roles).issubset({'ANALYST','SEARCHER','REVIEWER','EDITOR','LEAD'}):parser.error('--name과 유효한 --roles가 필요합니다.')
                uid,token=create_user(c,args.name,roles);print(f'사용자 {uid}, 12시간 유효 토큰(1회 표시): {token}')
            elif args.command=='issue-token':
                users=rows(c,'app_user',login_subject=args.name)
                if not users:parser.error('사용자를 찾을 수 없습니다.')
                print(issue_token(c,users[0]['user_id']))
            else:print('기준 선거·지표·FC01~FC20 초기화 완료.')

if __name__=='__main__':main()
