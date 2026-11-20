"""Durable database queue; run with python -m app.worker."""
import csv
import hashlib
import io
import json
import statistics
import time
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from .db import T, now, rows, one, add, update, create_engine, initialize
from .service import safe_file, bump, audit

YEARS=(2016,2020,2024)
METRICS=[('TURNOUT','PERCENT','투표자수 / 선거인수 × 100'),('CHANGE','PP','후년 투표율 − 전년 투표율'),
 ('NATIONAL_GAP','PP','지역 투표율 − 해당 연도 공식 전국 투표율'),('REPEATED_LOW','COUNT','세 선거 모두 같은 해 전국 투표율 미만이면 1'),
 ('STDDEV','PP','공통 지역 동일 가중 모집단 표준편차'),('P90_P10','PP','선형 보간 백분위 90 − 10'),
 ('RANGE','PP','공통 지역 최고 투표율 − 최저 투표율'),('DECLINED','COUNT','비교 기간 투표율 하락 지역 수')]

def read_csv(file):
    text=safe_file(file).read_text(encoding='utf-8-sig')
    reader=csv.DictReader(io.StringIO(text))
    required={'election_year','province','region_code','region_name','eligible_voters','voters','official_turnout'}
    if not required.issubset(reader.fieldnames or []): raise ValueError('CSV 필수 열: '+', '.join(sorted(required)))
    values=list(reader)
    if len(values)>10000: raise ValueError('CSV는 10000행 이하여야 합니다.')
    return values

def import_data(c,job):
    payload=job['payload']; did=payload['target_id']; dataset=one(c,'dataset_version',did)
    if dataset['status']!='DRAFT' or dataset['revision_token']!=payload['token']: raise ValueError('작업본 버전이 바뀌었습니다.')
    if rows(c,'turnout_observation',dataset_id=did): raise ValueError('이미 관측이 있는 버전입니다. 새 작업본을 생성하세요.')
    elections={x['election_year']:x['election_id'] for x in rows(c,'election')}
    seen=set(); national=set(); regions=set()
    for fid in payload['file_ids']:
        file=one(c,'source_file',fid)
        for index,r in enumerate(read_csv(file),2):
            year=int(r['election_year']); eligible=int(r['eligible_voters']); voters=int(r['voters'])
            rate=Decimal(r['official_turnout'])
            if year not in YEARS or eligible<=0 or not 0<=voters<=eligible or not rate.is_finite() or not 0<=rate<=100: raise ValueError(f'{index}행 범위 오류')
            code=r['region_code'].strip()
            if not code or not r['region_name'].strip(): raise ValueError(f'{index}행 지역 코드·이름 누락')
            if (year,code) in seen: raise ValueError(f'{index}행 선거·지역 중복')
            seen.add((year,code))
            if code=='NATIONAL':
                national.add(year)
                add(c,'region_mapping',dataset_id=did,election_id=elections[year],file_id=fid,source_row_key=str(index),original_region='NATIONAL',region_id=None,handling_note='공식 전국 집계: 공통 비교 지역 집합과 분리')
                continue
            province=r['province'].strip()
            if not province: raise ValueError(f'{index}행 시도 누락')
            parent=rows(c,'region',region_code='PROVINCE-'+province)
            parent_id=parent[0]['region_id'] if parent else add(c,'region',region_code='PROVINCE-'+province,parent_id=None,name=province,region_level='PROVINCE')
            region=rows(c,'region',region_code=code)
            region_id=region[0]['region_id'] if region else add(c,'region',region_code=code,parent_id=parent_id,name=r['region_name'].strip(),region_level='SIGUNGU')
            if region and (region[0]['name']!=r['region_name'].strip() or region[0]['parent_id']!=parent_id): raise ValueError(f'{index}행 통일 지역 정보 충돌: 지역대응 검토 필요')
            mapping=add(c,'region_mapping',dataset_id=did,election_id=elections[year],file_id=fid,source_row_key=str(index),original_region=province+' '+r['region_name'],region_id=region_id,handling_note='동일 코드·경계라는 수집자 확인을 전제로 한 직접 대응; 경계 변경은 별도 제외 검토 필요')
            decimals=max(0,-rate.as_tuple().exponent)
            if decimals>6: raise ValueError('공식 소수 자리수는 최대 6자리입니다.')
            obs=add(c,'turnout_observation',dataset_id=did,election_id=elections[year],region_id=region_id,eligible_voters=eligible,voters=voters,official_turnout=rate,official_decimals=decimals,calculated_turnout=Decimal(voters)*100/eligible)
            add(c,'observation_lineage',observation_id=obs,mapping_id=mapping,contribution_note='원본 행 직접 연결')
            regions.add(region_id)
    if national!=set(YEARS): raise ValueError('NATIONAL 코드의 공식 전국 합계 행이 세 연도 모두 필요합니다.')
    for rid in regions:
        included=len(rows(c,'turnout_observation',dataset_id=did,region_id=rid))==3
        add(c,'comparison_member',dataset_id=did,region_id=rid,is_included=included,reason='세 선거 관측 존재' if included else '선거 관측 누락으로 비교 제외')
    bump(c,'dataset_version',did)
    return {'resource_type':'DATASET','resource_id':str(did),'ids':[],'summary':{'observations':len(seen)-3}}

def quality(c,job):
    did=job['payload']['target_id']; count=0
    for old in rows(c,'quality_issue',dataset_id=did):
        if old['status']=='OPEN': update(c,'quality_issue',old['issue_id'],status='RESOLVED',resolution='재검사로 이전 검사 종료')
    for obs in rows(c,'turnout_observation',dataset_id=did):
        tolerance=Decimal('0.5')*(Decimal(10)**-obs['official_decimals'])
        delta=abs(obs['official_turnout']-Decimal(obs['voters'])*100/obs['eligible_voters'])
        if delta>tolerance+Decimal('0.00000001'):
            add(c,'quality_issue',dataset_id=did,file_id=None,locator=str(obs['observation_id']),rule_code='ROUNDING',details={'difference':str(delta),'tolerance':str(tolerance)},status='OPEN',resolution=None); count+=1
    for fid in {m['file_id'] for m in rows(c,'region_mapping',dataset_id=did)}:
        for r in read_csv(one(c,'source_file',fid)):
            if r['region_code']=='NATIONAL':
                expected=Decimal(r['voters'])*100/Decimal(r['eligible_voters']); official=Decimal(r['official_turnout'])
                if abs(expected-official)>Decimal('.5')*(Decimal(10)**official.as_tuple().exponent):
                    add(c,'quality_issue',dataset_id=did,file_id=fid,locator='NATIONAL:'+r['election_year'],rule_code='NATIONAL_ROUNDING',details={'expected':str(expected),'official':str(official)},status='OPEN',resolution=None); count+=1
    add(c,'quality_issue',dataset_id=did,file_id=None,locator='DATASET',rule_code='CHECK_COMPLETE',details={'rule_version':job['payload']['rule_version'],'open_count':count,'content_token':one(c,'dataset_version',did)['revision_token']+1},status='RESOLVED',resolution='검사 실행 완료')
    bump(c,'dataset_version',did)
    return {'resource_type':'DATASET','resource_id':str(did),'ids':[],'summary':{'open_issues':count}}

def percentile(values,p):
    values=sorted(values); k=(len(values)-1)*p; lo=int(k); hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(k-lo)

def analyze(c,job):
    payload=job['payload']; run=one(c,'analysis_run',payload['run_id']); did=run['dataset_id']
    metrics={x['metric_code']:x['metric_id'] for x in rows(c,'metric_definition')}
    national={}; source_refs={}
    for fid in {m['file_id'] for m in rows(c,'region_mapping',dataset_id=did)}:
        for index,r in enumerate(read_csv(one(c,'source_file',fid)),2):
            if r['region_code']=='NATIONAL':
                year=int(r['election_year']); national[year]=Decimal(r['voters'])*100/Decimal(r['eligible_voters']); source_refs[year]={'file_id':str(fid),'locator':str(index)}
    if set(national)!=set(YEARS): raise ValueError('공식 전국 합계가 없습니다.')
    included={r['region_id'] for r in rows(c,'comparison_member',dataset_id=did) if r['is_included']}
    obs=[x for x in rows(c,'turnout_observation',dataset_id=did) if x['region_id'] in included]
    if not included: raise ValueError('비교할 공통 지역이 없습니다.')
    elections={x['election_id']:x['election_year'] for x in rows(c,'election')}
    by={(x['region_id'],elections[x['election_id']]):x for x in obs}
    def result(code,value,rid=None,year=None,years=None,extra=None):
        eid=next((e for e,y in elections.items() if y==year),None)
        context={'years':years or [year],'region_count':len(included),'basis':'COMMON_REGIONS','unit':next(m[1] for m in METRICS if m[0]==code),'national_source':source_refs.get(year),**(extra or {})}
        return add(c,'analysis_result',run_id=run['run_id'],metric_id=metrics[code],dimension_key=f'{code}:{rid or "NATIONAL"}:{years or year}',election_id=eid,region_id=rid,value=Decimal(str(value)),context=context)
    for year in YEARS:
        rates=[Decimal(by[rid,year]['voters'])*100/by[rid,year]['eligible_voters'] for rid in included]
        result('TURNOUT',national[year],year=year,extra={'basis':'NATIONAL_OFFICIAL'})
        result('STDDEV',statistics.pstdev(rates),year=year)
        result('P90_P10',percentile(rates,Decimal('.9'))-percentile(rates,Decimal('.1')),year=year)
        result('RANGE',max(rates)-min(rates),year=year)
        for rid in sorted(included):
            x=by[rid,year]; rate=Decimal(x['voters'])*100/x['eligible_voters']
            result('TURNOUT',rate,rid,year)
            result('NATIONAL_GAP',rate-national[year],rid,year)
    for a,b in [(2016,2020),(2020,2024),(2016,2024)]: result('CHANGE',national[b]-national[a],years=[a,b],extra={'basis':'NATIONAL_OFFICIAL'})
    result('REPEATED_LOW',sum(all(Decimal(by[r,y]['voters'])*100/by[r,y]['eligible_voters']<national[y] for y in YEARS) for r in included),years=list(YEARS))
    result('DECLINED',sum(Decimal(by[r,2024]['voters'])/by[r,2024]['eligible_voters']<Decimal(by[r,2020]['voters'])/by[r,2020]['eligible_voters'] for r in included),years=[2020,2024])
    for rid in sorted(included):
        rates={y:Decimal(by[rid,y]['voters'])*100/by[rid,y]['eligible_voters'] for y in YEARS}
        result('REPEATED_LOW',int(all(rates[y]<national[y] for y in YEARS)),rid,years=list(YEARS))
        for a,b in [(2016,2020),(2020,2024),(2016,2024)]: result('CHANGE',rates[b]-rates[a],rid,years=[a,b])
    update(c,'analysis_run',run['run_id'],status='SUCCEEDED')
    return {'resource_type':'ANALYSIS_RUN','resource_id':str(run['run_id']),'ids':[],'summary':{'results':len(rows(c,'analysis_result',run_id=run['run_id']))}}

def rule_check(c,job):
    rid=job['payload']['target_id']; version=job['payload']['rule_version']; rev=one(c,'article_revision',rid)
    body=json.loads(rev['body']); texts={b['block_id']:b.get('text') or '' for b in body['blocks']}
    claims=rows(c,'claim',revision_id=rid); failures={k:[] for k in ('NUMBER','DATE','TARGET','SOURCE','UNIT')}
    import re
    # These deterministic checks find verifiable mismatches; human review remains mandatory.
    for cl in claims:
        links=rows(c,'claim_evidence',claim_id=cl['claim_id'])
        try:
            block,a,b=cl['body_locator'].rsplit(':',2)
            if texts[block][int(a):int(b)]!=cl['text']: failures['TARGET'].append(cl['claim_id'])
        except (KeyError,ValueError): failures['TARGET'].append(cl['claim_id'])
        if cl['is_numeric'] and not links: failures['SOURCE'].append(cl['claim_id'])
        values=[]; evidence_years=set()
        for link in links:
            if link['result_id']:
                r=one(c,'analysis_result',link['result_id']); metric=one(c,'metric_definition',r['metric_id']); values.append(r['value'])
                if metric['unit']=='PP' and '%p' not in cl['text']: failures['UNIT'].append(cl['claim_id'])
                if metric['unit']=='PERCENT' and ('%' not in cl['text'] or '%p' in cl['text']): failures['UNIT'].append(cl['claim_id'])
                evidence_years.update(r['context']['years'])
        mentioned=set(map(int,re.findall(r'(?<!\d)(2016|2020|2024)(?!\d)',cl['text'])))
        if evidence_years and not mentioned.issubset(evidence_years): failures['DATE'].append(cl['claim_id'])
        if values:
            numbers=[Decimal(v) for v in re.findall(r'(-?\d+(?:\.\d+)?)\s*(?:%p|%)(?![A-Za-z0-9])',cl['text'])]
            for v in numbers:
                tolerance=Decimal('.5')*(Decimal(10)**v.as_tuple().exponent)
                if not any(abs(v-x)<=tolerance for x in values): failures['NUMBER'].append(cl['claim_id'])
    for category,failed in failures.items():
        add(c,'rule_check',revision_id=rid,rule_version=version,category=category,body_locator='ALL',status='FAIL' if failed else 'PASS',details={'claim_ids':failed,'scope':'deterministic-v1; independent human review required'},checked_at=now())
    return {'resource_type':'ARTICLE_REVISION','resource_id':str(rid),'ids':[],'summary':{'rule_version':version,'failures':sum(map(len,failures.values()))}}

def execute(c,job):
    payload=job['payload']; kind=job['type']
    if kind=='IMPORT': return import_data(c,job)
    if kind=='QUALITY_CHECK': return quality(c,job)
    if kind=='ANALYSIS': return analyze(c,job)
    if kind=='RULE_CHECK': return rule_check(c,job)
    if kind=='EXTRACTION':
        file=one(c,'source_file',payload['file_id']); text=safe_file(file).read_text(encoding='utf-8-sig')
        if len(text)>200000: raise ValueError('텍스트 근거는 200000자 이하만 지원합니다.')
        ids=[str(add(c,'evidence_chunk',file_id=file['file_id'],locator=f'characters:{i}:{min(i+2000,len(text))}',content=text[i:i+2000],is_sample=payload.get('is_sample',False),created_at=now())) for i in range(0,len(text),2000)]
        return {'resource_type':'EVIDENCE','resource_id':None,'ids':ids,'summary':{}}
    if kind=='FILE_VERIFICATION':
        for fid in payload['file_ids']:
            file=one(c,'handover_file',fid)
            if file['publication_id']!=payload['target_id']: raise ValueError('다른 공개 묶음의 파일입니다.')
            data=safe_file(file).read_bytes()
            if hashlib.sha256(data).hexdigest()!=file['sha256']: raise ValueError('파일 해시가 일치하지 않습니다.')
            update(c,'handover_file',fid,verified_at=now())
        bump(c,'publication',payload['target_id'])
        return {'resource_type':'PUBLICATION','resource_id':str(payload['target_id']),'ids':[],'summary':{}}
    if kind=='ARTICLE_DRAFT':
        results=rows(c,'analysis_result',run_id=payload['run_id'])[:5]
        blocks=[]; claims=[]
        for n,r in enumerate(results,1):
            metric=one(c,'metric_definition',r['metric_id']); name=one(c,'region',r['region_id'])['name'] if r['region_id'] else '전체 기준'
            text=f"{r['context']['years']} {name} {metric['metric_code']}: {r['value']:.2f}{'%p' if metric['unit']=='PP' else '%'}"
            bid=f'p{n}';blocks.append({'block_id':bid,'type':'PARAGRAPH','text':text})
            claims.append({'claim_no':n,'text':text,'body_locator':f'{bid}:0:{len(text)}','is_core':True,'is_numeric':True,'evidence':[{'result_id':str(r['result_id']),'note':'계산 결과 직접 연결','relation_type':'SUPPORT'}]})
        return {'resource_type':'DRAFT_PROPOSAL','resource_id':None,'ids':[],'summary':{'body':{'blocks':blocks},'claims':claims,'held_statements':['자동 초안은 검토 전입니다. 원인·인과 해설은 보류합니다.'],'generator':'deterministic-template-v1'}}
    raise ValueError('지원하지 않는 작업 종류입니다.')

def tick(engine):
    worker_id=str(uuid.uuid4())
    with engine.begin() as c:
        if engine.dialect.name=='postgresql': c.execute(sa.text('SELECT pg_advisory_xact_lock(7102016)'))
        pending=c.execute(sa.select(T['api_job']).where(sa.or_(T['api_job'].c.status=='QUEUED',sa.and_(T['api_job'].c.status=='RUNNING',T['api_job'].c.lease_until<now()))).order_by(T['api_job'].c.created_at).with_for_update(skip_locked=True)).mappings().first()
        if not pending: return False
        job=dict(pending)
        if job['attempts']>=3:
            update(c,'api_job',job['job_id'],status='FAILED',error={'code':'LEASE_EXHAUSTED','message':'작업 복구 횟수를 초과했습니다.','retryable':False},finished_at=now());return True
        update(c,'api_job',job['job_id'],status='RUNNING',lease_owner=worker_id,lease_until=now()+timedelta(minutes=5),attempts=job['attempts']+1,progress=10)
    try:
        with engine.begin() as c:
            if engine.dialect.name=='postgresql': c.execute(sa.text('SELECT pg_advisory_xact_lock(7102016)'))
            current=one(c,'api_job',job['job_id'])
            if current['lease_owner']!=worker_id: return True
            if job['payload'].get('target_table') in ('dataset_version','publication'):
                parent=one(c,job['payload']['target_table'],job['payload']['target_id'])
                if parent['status']!='DRAFT' or parent['revision_token']!=job['payload']['token']: raise ValueError('입력 작업본이 바뀌었습니다.')
            result=execute(c,job)
            update(c,'api_job',job['job_id'],status='SUCCEEDED',result=result,progress=100,finished_at=now(),lease_until=None)
            audit(c,{'user_id':job['actor_id']},'api_job',job['job_id'],'COMPLETE',{'type':job['type']})
    except Exception as exc:
        with engine.begin() as c:
            update(c,'api_job',job['job_id'],status='FAILED',error={'code':'JOB_FAILED','message':str(exc)[:400],'retryable':False},finished_at=now(),lease_until=None)
            if job['type']=='ANALYSIS': update(c,'analysis_run',job['payload']['run_id'],status='FAILED')
    return True

if __name__=='__main__':
    engine=create_engine();initialize(engine)
    print('Worker ready. Ctrl+C to stop.',flush=True)
    while True:
        try:
            if not tick(engine): time.sleep(1)
        except KeyboardInterrupt: break
