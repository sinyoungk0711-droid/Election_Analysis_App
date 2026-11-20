import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI, Request, UploadFile, File, Form, Query, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from .db import T, DATA, now, initialize, create_engine, rows, one, add, update, serialize
from .contracts import *
from .service import *
from .worker import METRICS, YEARS

def create_app(engine=None):
    engine=engine or create_engine(); initialize(engine)
    app=FastAPI(title='총선 탐사보도 API',version='0.1.0',description='실행 가능한 초기 구현. 설계 대비 지원 범위는 docs/IMPLEMENTATION.md 참조.')
    app.state.engine=engine
    secret_path=DATA/'signing-key'
    if not secret_path.exists(): secret_path.write_text(secrets.token_hex(32),encoding='ascii')
    signing_key=secret_path.read_bytes()
    hits=defaultdict(deque)

    def response(data,request,status=200,headers=None,page=None):
        body={'data':serialize(data),'meta':{'request_id':request.state.request_id,'publication_id':request.query_params.get('publication_id')}}
        if page is not None: body['page']=page
        return JSONResponse(body,status_code=status,headers=headers or {})

    def collection(data,request):
        try: limit=int(request.query_params.get('limit','20'))
        except ValueError: fail(422,'VALIDATION_FAILED','limit는 정수여야 합니다.')
        if not 1<=limit<=100: fail(422,'VALIDATION_FAILED','limit는 1~100입니다.')
        scope=hashlib.sha256((request.url.path+str(sorted((k,v) for k,v in request.query_params.multi_items() if k!='cursor'))).encode()).hexdigest()[:20]
        def rowkey(r):
            return str(next((v for k,v in r.items() if k.endswith('_id') and v is not None),r.get('fc_code',r.get('region_code',r))))
        cursor=request.query_params.get('cursor'); start=0
        if cursor:
            try:
                raw,signature=cursor.split('.')
                if not hmac.compare_digest(hmac.new(signing_key,raw.encode(),'sha256').hexdigest(),signature): raise ValueError()
                val=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
                if val['scope']!=scope: raise ValueError()
                start=next(i+1 for i,r in enumerate(data) if rowkey(r)==val['after'])
            except Exception: fail(400,'INVALID_CURSOR','이 조회 조건에 사용할 수 없는 커서입니다.')
        selection=data[start:start+limit]; more=start+limit<len(data); next_cursor=None
        if more:
            raw=base64.urlsafe_b64encode(json.dumps({'scope':scope,'after':rowkey(selection[-1])}).encode()).decode().rstrip('=')
            next_cursor=raw+'.'+hmac.new(signing_key,raw.encode(),'sha256').hexdigest()
        return response(selection,request,page={'limit':limit,'next_cursor':next_cursor,'has_more':more})

    def problem(request,status,detail):
        if not isinstance(detail,dict): detail={'detail':str(detail),'code':'HTTP_ERROR'}
        return JSONResponse({'type':'about:blank','title':'요청을 처리할 수 없습니다.','status':status,
          'instance':request.url.path,'request_id':getattr(request.state,'request_id',''),
          'errors':[],'blocking_issues':[],**detail},status_code=status,media_type='application/problem+json',
          headers={'Cache-Control':'no-store','X-Request-ID':getattr(request.state,'request_id',''),**({'WWW-Authenticate':'Bearer'} if status==401 else {}),**({'Retry-After':'5'} if status==429 else {})})

    @app.exception_handler(HTTPException)
    async def http_error(request,exc): return problem(request,exc.status_code,exc.detail)
    @app.exception_handler(KeyError)
    async def missing(request,exc): return problem(request,404,{'code':'NOT_FOUND','detail':'자료를 찾을 수 없습니다.'})
    @app.exception_handler(RequestValidationError)
    async def invalid(request,exc): return problem(request,422,{'code':'VALIDATION_FAILED','detail':'입력 항목을 확인하세요.','errors':[{'field':'.'.join(map(str,e['loc'])),'code':e['type'],'message':e['msg']} for e in exc.errors()]})
    @app.exception_handler(sa.exc.IntegrityError)
    async def integrity(request,exc): return problem(request,409,{'code':'CONSTRAINT_CONFLICT','detail':'중복 키 또는 데이터 관계를 확인하세요.'})
    @app.exception_handler(ValueError)
    async def value_error(request,exc): return problem(request,422,{'code':'VALIDATION_FAILED','detail':str(exc)[:300]})

    @app.middleware('http')
    async def unit_of_work(request,call_next):
        request.state.request_id=str(uuid.uuid4())
        is_api=request.url.path.startswith('/api/')
        if not is_api:
            out=await call_next(request)
            out.headers['X-Content-Type-Options']='nosniff'
            out.headers['Referrer-Policy']='same-origin'
            return out
        write=request.method in ('POST','PUT','PATCH','DELETE')
        c=engine.connect(); trans=c.begin(); request.state.c=c
        try:
            if engine.dialect.name=='postgresql': c.execute(sa.text('SELECT pg_advisory_xact_lock(7102016)'))
            actor=None
            if request.url.path.startswith('/api/v1/admin/'):
                auth=request.headers.get('authorization','')
                if not auth.startswith('Bearer '): fail(401,'UNAUTHENTICATED','인증이 필요합니다.')
                actor=authenticate(c,auth[7:]); request.state.actor=actor
            identity=str(actor['user_id']) if actor else (request.client.host if request.client else 'local')
            window=hits[(identity,'write' if write else 'read')]; stamp=time.monotonic()
            while window and stamp-window[0]>60: window.popleft()
            if len(window)>=(30 if write else 120): fail(429,'RATE_LIMITED','잠시 후 다시 시도하세요.')
            window.append(stamp)
            key=None; digest=None; method_path=request.method+' '+request.url.path
            if write and actor:
                key=request.headers.get('idempotency-key')
                try: uuid.UUID(key or '')
                except ValueError: fail(422,'VALIDATION_FAILED','Idempotency-Key UUID가 필요합니다.')
                max_bytes=51*1024*1024 if 'multipart/form-data' in request.headers.get('content-type','') else 2*1024*1024
                body=bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>max_bytes: fail(413,'PAYLOAD_TOO_LARGE','요청 크기 제한을 초과했습니다.')
                request._body=bytes(body)
                canonical=bytes(body)
                content_type=request.headers.get('content-type','')
                if 'application/json' in content_type and canonical.strip():
                    try: canonical=json.dumps(json.loads(canonical),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
                    except (ValueError,UnicodeDecodeError):fail(400,'INVALID_JSON','JSON 문법을 확인하세요.')
                elif 'multipart/form-data' in content_type:
                    parsed=await request.form();parts=[]
                    for name,value in parsed.multi_items():
                        if hasattr(value,'read'):
                            content=await value.read();await value.seek(0)
                            parts.append((name,value.filename,hashlib.sha256(content).hexdigest()))
                        else:parts.append((name,str(value)))
                    canonical=json.dumps(sorted(parts,key=lambda v:v[0]),ensure_ascii=False).encode()
                digest=hashlib.sha256(canonical).hexdigest()
                old=rows(c,'api_idempotency',actor_id=actor['user_id'],key=key,method_path=method_path)
                if old:
                    cached=old[0]
                    if cached['created_at'].replace(tzinfo=timezone.utc)<now()-timedelta(hours=24):
                        c.execute(T['api_idempotency'].delete().where(sa.and_(T['api_idempotency'].c.actor_id==actor['user_id'],T['api_idempotency'].c.key==key,T['api_idempotency'].c.method_path==method_path)))
                    else:
                        if cached['request_hash']!=digest: fail(409,'IDEMPOTENCY_CONFLICT','같은 키에 다른 요청을 보낼 수 없습니다.')
                        out=JSONResponse(cached['response']['body'],status_code=cached['http_status'],headers=cached['response'].get('headers',{}))
                        out.headers['Cache-Control']='no-store';out.headers['X-Request-ID']=request.state.request_id
                        trans.commit(); return out
            out=await call_next(request)
            if write and actor and out.status_code<400:
                content=b''.join([chunk async for chunk in out.body_iterator])
                payload=json.loads(content)
                saved_headers={k:v for k,v in out.headers.items() if k.lower() in ('location','etag','retry-after')}
                add(c,'api_idempotency',actor_id=actor['user_id'],key=key,method_path=method_path,request_hash=digest,response={'body':payload,'headers':saved_headers},http_status=out.status_code,created_at=now())
                audit(c,actor,'http',request.url.path,request.method,{'request_id':request.state.request_id,'status':out.status_code})
                out=JSONResponse(payload,status_code=out.status_code,headers=saved_headers)
            if out.status_code>=400: trans.rollback()
            else: trans.commit()
            out.headers['X-Request-ID']=request.state.request_id
            if out.headers.get('Cache-Control')!='no-store': out.headers['Cache-Control']='no-store' if actor else 'public, max-age=0, must-revalidate'
            out.headers['X-Content-Type-Options']='nosniff'
            if out.status_code==429: out.headers['Retry-After']='5'
            return out
        except HTTPException as exc:
            if trans.is_active: trans.rollback()
            return problem(request,exc.status_code,exc.detail)
        except Exception:
            if trans.is_active: trans.rollback()
            import logging
            logging.exception('request_failed request_id=%s',request.state.request_id)
            return problem(request,500,{'code':'INTERNAL_ERROR','detail':'내부 오류입니다. 요청 ID로 문의하세요.'})
        finally: c.close()

    def actor(request,*roles):
        a=request.state.actor
        if roles: require(a,*roles)
        return a
    def details(request,table,pk):
        row=one(request.state.c,table,pk); tag=etag(request.state.c,table,pk)
        row['etag']=tag
        kind=next((k for k,v in TARGETS.items() if v[0]==table),None)
        if kind: row['effective_approval']=approval(request.state.c,kind,pk)
        return response(row,request,headers={'ETag':tag})
    def jobs(request,kind,payload):
        job=enqueue(request.state.c,request.state.actor,kind,payload)
        return response(job,request,202,{'Location':'/api/v1/admin/jobs/'+job['job_id'],'Retry-After':'2'})
    def protect(request,table,pk):
        precondition(request,table,pk); return mutable(request.state.c,table,pk)
    def pid(request):
        value=request.query_params.get('publication_id')
        if not value or not value.isdecimal(): fail(422,'VALIDATION_FAILED','publication_id가 필요합니다.')
        published(request.state.c,int(value));return int(value)
    def public_revisions(c,p): return [i['revision_id'] for i in public_items(c,p) if i['revision_id']]
    def card(c,rid):
        r=one(c,'article_revision',rid); article=one(c,'article',r['article_id'])
        return {'article_id':article['article_id'],'revision_id':rid,'slug':article['slug'],'title':r['title'],'summary':' '.join(b.get('text') or '' for b in json.loads(r['body'])['blocks'])[:160],'version_no':r['version_no']}
    def point(c,r):
        m=one(c,'metric_definition',r['metric_id']); e=one(c,'election',r['election_id']) if r['election_id'] else None
        return {'result_id':r['result_id'],'election_year':e['election_year'] if e else None,'metric_code':m['metric_code'],'value':r['value'],'unit':m['unit'],'status':'AVAILABLE' if r['value'] is not None else 'MISSING','reason':None}

    @app.get('/health')
    def health(): return {'status':'ok','version':'0.1.0'}
    @app.get('/api/v1/demo/accounts',include_in_schema=False)
    def demo_accounts(request:Request):
        if os.getenv('APP_DEMO_MODE')!='1' or not (DATA/'demo-accounts.json').exists(): fail(404,'NOT_FOUND','데모 모드가 아닙니다.')
        # Demo mode is explicitly insecure for deployment and disabled by default.
        return response(json.loads((DATA/'demo-accounts.json').read_text(encoding='utf-8')),request)

    @app.get('/api/v1/public/context')
    def context(request:Request):
        c=request.state.c; p=published(c)
        return response({**{k:p[k] for k in ('publication_id','version_no','published_at')},'run_ids':sorted(publication_runs(c,p['publication_id'])),'is_demo':(DATA/'DEMO').exists()},request)
    @app.get('/api/v1/public/elections')
    def elections(request:Request): pid(request);return response(rows(request.state.c,'election'),request)
    @app.get('/api/v1/public/regions')
    def regions(request:Request,q:str='',parent_id:int|None=None,level:str|None=None):
        pid(request);c=request.state.c; items=rows(c,'region')
        out=[]
        for r in items:
            if q and q.casefold() not in r['name'].casefold():continue
            if parent_id and r['parent_id']!=parent_id:continue
            if level and r['region_level']!=level:continue
            r['parent_name']=one(c,'region',r['parent_id'])['name'] if r['parent_id'] else None;out.append(r)
        return collection(sorted(out,key=lambda r:r['region_code']),request)
    @app.get('/api/v1/public/summary')
    def summary(request:Request):
        c=request.state.c;p=pid(request);runs=publication_runs(c,p)
        results=[r for run in runs for r in rows(c,'analysis_result',run_id=run) if r['region_id'] is None]
        return response({'national_turnout':[point(c,r) for r in results if one(c,'metric_definition',r['metric_id'])['metric_code']=='TURNOUT'],
           'gap_summary':[point(c,r) for r in results if one(c,'metric_definition',r['metric_id'])['metric_code']!='TURNOUT'],
           'featured_articles':[card(c,x) for x in public_revisions(c,p)],'chart_ids':[x['chart_id'] for x in public_items(c,p) if x['chart_id']]},request)
    @app.get('/api/v1/public/articles')
    def articles(request:Request,q:str=''):
        c=request.state.c;items=[card(c,r) for r in public_revisions(c,pid(request))]
        return collection([x for x in items if q.casefold() in x['title'].casefold()],request)
    @app.get('/api/v1/public/articles/{slug}')
    def public_article(slug:str,request:Request):
        c=request.state.c;p=pid(request)
        for r in public_revisions(c,p):
            out=card(c,r)
            if out['slug']==slug:
                rev=one(c,'article_revision',r);pub=one(c,'publication',p)
                out.update(body=json.loads(rev['body']),claim_ids=[x['claim_id'] for x in rows(c,'claim',revision_id=r)],published_at=pub['published_at'],correction_note=pub['correction_note'])
                return response(out,request)
        fail(404,'NOT_FOUND','해당 공개본에 없는 기사입니다.')
    @app.get('/api/v1/public/regions/{region_id}/turnout')
    def region_turnout(region_id:int,request:Request):
        c=request.state.c;p=pid(request);runs=publication_runs(c,p)
        if len(runs)!=1: fail(409,'MIXED_ANALYSIS_VERSION','분석 기준이 일치하지 않습니다.')
        run=one(c,'analysis_run',next(iter(runs)));did=run['dataset_id']; results=rows(c,'analysis_result',run_id=run['run_id'])
        members=rows(c,'comparison_member',dataset_id=did,region_id=region_id)
        if not members: fail(404,'NOT_FOUND','자료가 없는 지역입니다.')
        series=[]
        for year in YEARS:
            def find(code,rid):
                matches=[r for r in results if r['region_id']==rid and r['context']['years']==[year] and one(c,'metric_definition',r['metric_id'])['metric_code']==code]
                return point(c,matches[0]) if matches else {'result_id':None,'election_year':year,'metric_code':code,'value':None,'unit':'PP' if code!='TURNOUT' else 'PERCENT','status':'EXCLUDED','reason':members[0]['reason']}
            series.append({'election_year':year,'turnout':find('TURNOUT',region_id),'national_turnout':find('TURNOUT',None),'national_gap':find('NATIONAL_GAP',region_id)})
        return response({'region':one(c,'region',region_id),'comparison_status':'INCLUDED' if members[0]['is_included'] else 'EXCLUDED','reason':members[0]['reason'],'series':series},request)
    @app.get('/api/v1/public/comparisons')
    def comparisons(request:Request,metric_code:str,years:list[int]=Query(...),region_ids:list[int]=Query(default=[]),parent_id:int|None=None):
        c=request.state.c;p=pid(request)
        if len(region_ids)>5 or not years or not set(years).issubset(YEARS) or len(set(years))!=len(years): fail(422,'VALIDATION_FAILED','지역은 최대 5개, 연도는 2016·2020·2024를 중복 없이 지정하세요.')
        if metric_code=='CHANGE' and (len(years)!=2 or years!=sorted(years)):fail(422,'VALIDATION_FAILED','변화는 두 연도를 오름차순으로 지정하세요.')
        if metric_code=='REPEATED_LOW' and sorted(years)!=list(YEARS):fail(422,'VALIDATION_FAILED','반복 저참여는 세 연도가 필요합니다.')
        if metric_code in ('STDDEV','P90_P10') and (region_ids or parent_id):fail(422,'VALIDATION_FAILED','분포 지표는 전체 공통 지역 집합을 사용합니다.')
        metrics=rows(c,'metric_definition',metric_code=metric_code)
        if not metrics:fail(422,'VALIDATION_FAILED','지원하지 않는 지표입니다.')
        mids={m['metric_id'] for m in metrics};out=[]
        for run in publication_runs(c,p):
            for r in rows(c,'analysis_result',run_id=run):
                if r['metric_id'] not in mids:continue
                ry=r['context']['years']
                if metric_code in ('CHANGE','REPEATED_LOW'):
                    if ry!=years:continue
                elif ry[0] not in years:continue
                region=one(c,'region',r['region_id']) if r['region_id'] else None
                if region_ids and (not region or region['region_id'] not in region_ids):continue
                if parent_id and (not region or region['parent_id']!=parent_id):continue
                out.append({'result_id':r['result_id'],'region':region,'years':ry,'point':point(c,r),'context':r['context']})
        return collection(out,request)
    @app.get('/api/v1/public/charts/{chart_id}')
    def public_chart(chart_id:int,request:Request):
        c=request.state.c;p=pid(request)
        if chart_id not in [i['chart_id'] for i in public_items(c,p)]:fail(404,'NOT_FOUND','공개되지 않은 도표입니다.')
        chart=one(c,'chart',chart_id);chart.pop('created_by',None)
        chart['rows']=[{**one(c,'analysis_result',l['result_id']),'metric':one(c,'metric_definition',one(c,'analysis_result',l['result_id'])['metric_id'])} for l in rows(c,'chart_result',chart_id=chart_id)]
        return response(chart,request)
    def public_fc(c,p,fc):
        claimids={cl['claim_id'] for rid in public_revisions(c,p) for cl in rows(c,'claim',revision_id=rid)}
        checks=[]
        for r in [x for cid in claimids for x in current_fact_checks(c,cid) if x['fc_id']==fc['fc_id']]:
            if r['claim_id'] in claimids and approved(c,'FACT_CHECK_REVISION',r['fc_revision_id']):
                checks.append({'claim_id':r['claim_id'],'claim_text':one(c,'claim',r['claim_id'])['text'],'method':r['method'],'finding':r['finding'],'verdict':r['verdict'],'approved_at':approval(c,'FACT_CHECK_REVISION',r['fc_revision_id'])['decided_at']})
        return {'fc_code':fc['fc_code'],'question':fc['question'],'claim_checks':checks}
    @app.get('/api/v1/public/fact-checks')
    def fact_checks_public(request:Request,q:str='',verdict:str|None=None):
        c=request.state.c;p=pid(request);out=[public_fc(c,p,fc) for fc in rows(c,'fact_check')]
        return collection([x for x in out if x['claim_checks'] and q in (x['question']+x['fc_code']) and (not verdict or any(y['verdict']==verdict for y in x['claim_checks']))],request)
    @app.get('/api/v1/public/fact-checks/{fc_code}')
    def public_fc_detail(fc_code:str,request:Request):
        c=request.state.c;p=pid(request);fs=rows(c,'fact_check',fc_code=fc_code)
        if not fs:fail(404,'NOT_FOUND','검증 항목이 없습니다.')
        return response(public_fc(c,p,fs[0]),request)
    @app.get('/api/v1/public/claims/{claim_id}/evidence')
    def claim_evidence_public(claim_id:int,request:Request):
        c=request.state.c;p=pid(request);claim=one(c,'claim',claim_id)
        if claim['revision_id'] not in public_revisions(c,p):fail(404,'NOT_FOUND','공개되지 않은 주장입니다.')
        out=[]
        for link in rows(c,'claim_evidence',claim_id=claim_id):
            if link['result_id']:
                result=one(c,'analysis_result',link['result_id']);out.append({'kind':'RESULT','result_id':result['result_id'],'evidence_id':None,'relation_type':link['relation_type'],'value':result['value'],'locator':f"run:{result['run_id']}/result:{result['result_id']}",'context':result['context']})
            else:
                evidence=one(c,'evidence_chunk',link['evidence_id']);file=one(c,'source_file',evidence['file_id']);src=one(c,'source_document',file['source_id'])
                out.append({'kind':'EVIDENCE','evidence_id':evidence['evidence_id'],'result_id':None,'relation_type':link['relation_type'],'title':src['title'],'publisher':src['publisher'],'source_url':src['source_url'],'locator':evidence['locator'],'excerpt':None,'usage_terms':src['usage_terms'],'is_sample':evidence['is_sample']})
        return response(out,request)
    @app.get('/api/v1/public/methodology')
    def methodology(request:Request):
        c=request.state.c;p=pid(request);runs=publication_runs(c,p)
        datasets={one(c,'analysis_run',r)['dataset_id'] for r in runs}
        mappings=[m for did in datasets for m in rows(c,'region_mapping',dataset_id=did)]
        sourceids={one(c,'source_file',m['file_id'])['source_id'] for m in mappings}
        return response({'metrics':rows(c,'metric_definition'),'region_handling':mappings,'sources':[one(c,'source_document',s) for s in sourceids],'limitations':['집계 자료로 개인 행동과 인과관계를 단정할 수 없습니다.','전국 공식 합계와 공통 비교 지역의 분포를 구분합니다.'],'data_version':','.join(map(str,sorted(datasets)))},request)
    @app.get('/api/v1/public/files')
    def files_public(request:Request):
        c=request.state.c;p=pid(request)
        data=[]
        for r in rows(c,'handover_file',publication_id=p):
            if r['access_scope']!='PUBLIC':continue
            path=safe_file(r);r.update(name=f"result-{r['handover_file_id']}",size_bytes=path.stat().st_size)
            data.append(r)
        return collection(data,request)
    @app.get('/api/v1/public/files/{file_id}/download')
    def public_download(file_id:int,request:Request):
        c=request.state.c;p=pid(request);f=one(c,'handover_file',file_id)
        if f['publication_id']!=p or f['access_scope']!='PUBLIC':fail(404,'NOT_FOUND','공개 파일이 아닙니다.')
        # Serve after authorization instead of exposing local storage paths or reusable URLs.
        content=safe_file(f).read_bytes();extension='.pdf' if content.startswith(b'%PDF-') else '.docx' if content.startswith(b'PK') else '.bin'
        return Response(content,media_type='application/pdf' if extension=='.pdf' else 'application/octet-stream',headers={'Content-Disposition':f'attachment; filename="publication-{p}-file-{file_id}{extension}"','Cache-Control':'no-store'})
    @app.get('/api/v1/public/corrections')
    def corrections(request:Request):
        return collection([{'publication_id':p['publication_id'],'version_no':p['version_no'],'status':p['status'],'correction_note':p['correction_note'],'changed_at':p['published_at']} for p in reversed(rows(request.state.c,'publication')) if p['status'] in ('PUBLISHED','WITHDRAWN') and p['correction_note']],request)

    @app.get('/api/v1/admin/me')
    def me(request:Request):
        a=dict(actor(request));a['etag']=etag(request.state.c,'app_user',a['user_id']);return response(a,request,headers={'ETag':a['etag']})
    @app.get('/api/v1/admin/dashboard')
    def dashboard(request:Request):
        c=request.state.c
        return response({'datasets':len(rows(c,'dataset_version')),'runs':len(rows(c,'analysis_run')),'articles':len(rows(c,'article')),'open_issues':len(rows(c,'quality_issue',status='OPEN')),'fact_checks':len(rows(c,'fact_check')),'publications':rows(c,'publication'),'jobs':rows(c,'api_job')[-10:]},request)
    @app.get('/api/v1/admin/sources')
    def sources(request:Request,q:str=''):return collection([x for x in reversed(rows(request.state.c,'source_document')) if q in x['title']],request)
    @app.post('/api/v1/admin/sources',status_code=201)
    def source_create(body:SourceInput,request:Request):
        actor(request,'ANALYST','SEARCHER');values=body.model_dump()
        if values['published_on']:values['published_on']=date.fromisoformat(values['published_on'])
        sid=add(request.state.c,'source_document',**values)
        return response(one(request.state.c,'source_document',sid),request,201,{'Location':f'/api/v1/admin/sources?source_id={sid}'})
    @app.post('/api/v1/admin/source-files',status_code=201)
    async def source_upload(request:Request,source_id:int=Form(...),collection_scope:str=Form(...),file:UploadFile=File(...)):
        actor(request,'ANALYST','SEARCHER');c=request.state.c;one(c,'source_document',source_id)
        if Path(file.filename or '').suffix.lower() not in ('.csv','.txt','.md'):fail(415,'UNSUPPORTED_MEDIA_TYPE','현재 구현은 UTF-8 CSV·TXT·MD 파일을 지원합니다.')
        data=await file.read(50*1024*1024+1)
        if len(data)>50*1024*1024:fail(413,'PAYLOAD_TOO_LARGE','50MiB를 초과했습니다.')
        try:data.decode('utf-8-sig')
        except UnicodeDecodeError:fail(415,'UNSUPPORTED_MEDIA_TYPE','UTF-8 텍스트 파일이어야 합니다.')
        uri,digest=save_blob(data);fid=add(c,'source_file',source_id=source_id,storage_uri=uri,sha256=digest,collected_at=now(),collection_scope=collection_scope)
        return response(one(c,'source_file',fid),request,201,{'Location':f'/api/v1/admin/source-files/{fid}'})
    @app.get('/api/v1/admin/source-files/{file_id}')
    def source_detail(file_id:int,request:Request):return details(request,'source_file',file_id)
    @app.post('/api/v1/admin/source-files/{file_id}/extractions',status_code=202)
    def extraction(file_id:int,body:ExtractInput,request:Request):
        actor(request,'SEARCHER');one(request.state.c,'source_file',file_id)
        return jobs(request,'EXTRACTION',{'file_id':file_id,'extractor_version':body.extractor_version})
    @app.get('/api/v1/admin/datasets')
    def datasets(request:Request,status:str|None=None):
        data=rows(request.state.c,'dataset_version',**({'status':status} if status else {}));return collection(list(reversed(data)),request)
    @app.post('/api/v1/admin/datasets',status_code=201)
    def dataset_create(body:DatasetInput,request:Request):
        a=actor(request,'ANALYST');c=request.state.c
        if body.base_dataset_id: fail(422,'UNSUPPORTED_OPERATION','초기 구현은 파일 재가져오기로 새 기준본을 만듭니다. base_dataset_id를 생략하세요.')
        did=add(c,'dataset_version',version_no=max([x['version_no'] for x in rows(c,'dataset_version')],default=0)+1,status='DRAFT',created_by=a['user_id'],created_at=now(),frozen_at=None)
        audit(c,a,'dataset_version',did,'CREATE',{'change_note':body.change_note})
        return response(one(c,'dataset_version',did),request,201,{'Location':f'/api/v1/admin/datasets/{did}'})
    @app.get('/api/v1/admin/datasets/{dataset_id}')
    def dataset_detail(dataset_id:int,request:Request):return details(request,'dataset_version',dataset_id)
    @app.post('/api/v1/admin/datasets/{dataset_id}/imports',status_code=202)
    def importing(dataset_id:int,body:ImportInput,request:Request):
        actor(request,'ANALYST');r=protect(request,'dataset_version',dataset_id)
        for fid in body.file_ids:one(request.state.c,'source_file',fid)
        return jobs(request,'IMPORT',{'target_table':'dataset_version','target_id':dataset_id,'token':r['revision_token'],**body.model_dump()})
    @app.get('/api/v1/admin/datasets/{dataset_id}/mappings')
    def mappings(dataset_id:int,request:Request):return collection(rows(request.state.c,'region_mapping',dataset_id=dataset_id),request)
    @app.patch('/api/v1/admin/datasets/{dataset_id}/mappings/{mapping_id}')
    def mapping_patch(dataset_id:int,mapping_id:int,body:MappingInput,request:Request):
        actor(request,'ANALYST');protect(request,'dataset_version',dataset_id);c=request.state.c;m=one(c,'region_mapping',mapping_id)
        if m['dataset_id']!=dataset_id:fail(404,'NOT_FOUND','다른 데이터 버전입니다.')
        if body.region_id!=m['region_id']:fail(422,'UNSUPPORTED_OPERATION','지역 재구성은 원본 CSV의 통일 코드를 검토해 새 버전으로 다시 가져오세요.')
        update(c,'region_mapping',mapping_id,handling_note=body.handling_note);bump(c,'dataset_version',dataset_id)
        return response(one(c,'region_mapping',mapping_id),request)
    @app.get('/api/v1/admin/datasets/{dataset_id}/observations')
    def observations(dataset_id:int,request:Request,election_id:int|None=None,region_id:int|None=None):
        filters={k:v for k,v in {'election_id':election_id,'region_id':region_id}.items() if v is not None}
        return collection(rows(request.state.c,'turnout_observation',dataset_id=dataset_id,**filters),request)
    @app.put('/api/v1/admin/datasets/{dataset_id}/comparison-members')
    def members(dataset_id:int,body:MembersInput,request:Request):
        actor(request,'ANALYST');protect(request,'dataset_version',dataset_id);c=request.state.c
        if len({x.region_id for x in body.members})!=len(body.members):fail(422,'VALIDATION_FAILED','지역이 중복됩니다.')
        for m in body.members:
            one(c,'region',m.region_id)
            if m.is_included and len(rows(c,'turnout_observation',dataset_id=dataset_id,region_id=m.region_id))!=3:fail(422,'VALIDATION_FAILED','포함 지역에 세 선거 관측이 필요합니다.')
        c.execute(T['comparison_member'].delete().where(T['comparison_member'].c.dataset_id==dataset_id))
        for m in body.members:add(c,'comparison_member',dataset_id=dataset_id,**m.model_dump())
        bump(c,'dataset_version',dataset_id);return response({'included_count':sum(m.is_included for m in body.members),'excluded_count':sum(not m.is_included for m in body.members)},request)
    @app.post('/api/v1/admin/datasets/{dataset_id}/quality-checks',status_code=202)
    def quality_checks(dataset_id:int,body:CheckInput,request:Request):
        actor(request,'ANALYST','REVIEWER');r=protect(request,'dataset_version',dataset_id)
        return jobs(request,'QUALITY_CHECK',{'target_table':'dataset_version','target_id':dataset_id,'token':r['revision_token'],**body.model_dump()})
    @app.get('/api/v1/admin/datasets/{dataset_id}/quality-issues')
    def issues(dataset_id:int,request:Request,status:str|None=None):return collection(rows(request.state.c,'quality_issue',dataset_id=dataset_id,**({'status':status} if status else {})),request)
    @app.post('/api/v1/admin/datasets/{dataset_id}/freeze')
    def dataset_freeze(dataset_id:int,body:NoteInput,request:Request):
        actor(request,'ANALYST','LEAD');protect(request,'dataset_version',dataset_id);c=request.state.c;enforce_ready(c,'DATASET',dataset_id)
        if not approved(c,'DATASET',dataset_id):fail(409,'APPROVAL_REQUIRED','독립 데이터 승인이 필요합니다.')
        update(c,'dataset_version',dataset_id,status='FROZEN',frozen_at=now());return details(request,'dataset_version',dataset_id)

    @app.post('/api/v1/admin/analysis-runs',status_code=202)
    def analysis_create(body:AnalysisInput,request:Request):
        a=actor(request,'ANALYST');c=request.state.c;ds=one(c,'dataset_version',body.dataset_id)
        if ds['status']!='FROZEN' or not approved(c,'DATASET',body.dataset_id):fail(409,'APPROVAL_REQUIRED','고정·승인한 데이터를 사용하세요.')
        if body.parameters and (body.parameters.get('years',list(YEARS))!=list(YEARS) or body.parameters.get('baseline','NATIONAL_OFFICIAL')!='NATIONAL_OFFICIAL' or body.parameters.get('comparison_basis','COMMON_REGIONS')!='COMMON_REGIONS' or set(body.parameters)-{'years','baseline','comparison_basis'}):fail(422,'VALIDATION_FAILED','초기 분석은 세 선거·전체 지표를 고정 기준으로 실행합니다.')
        rid=add(c,'analysis_run',dataset_id=body.dataset_id,code_ref='builtin-v1',parameters={'years':list(YEARS),'baseline':'NATIONAL_OFFICIAL','comparison_basis':'COMMON_REGIONS'},executed_by=a['user_id'],executed_at=now(),status='RUNNING')
        return jobs(request,'ANALYSIS',{'run_id':rid})
    @app.get('/api/v1/admin/analysis-runs/{run_id}')
    def analysis_detail(run_id:int,request:Request):return details(request,'analysis_run',run_id)
    @app.get('/api/v1/admin/analysis-runs/{run_id}/results')
    def results(run_id:int,request:Request,metric_id:int|None=None,region_id:int|None=None,election_id:int|None=None):
        c=request.state.c;r=one(c,'analysis_run',run_id)
        if r['status']!='SUCCEEDED':fail(409,'JOB_NOT_READY','분석이 완료되지 않았습니다.')
        return collection(rows(c,'analysis_result',run_id=run_id,**{k:v for k,v in {'metric_id':metric_id,'region_id':region_id,'election_id':election_id}.items() if v is not None}),request)
    @app.get('/api/v1/admin/metrics')
    def metrics(request:Request,metric_code:str|None=None):return collection(rows(request.state.c,'metric_definition',**({'metric_code':metric_code} if metric_code else {})),request)
    @app.post('/api/v1/admin/charts',status_code=201)
    def chart_create(body:ChartInput,request:Request):
        a=actor(request,'ANALYST','EDITOR');c=request.state.c
        if len(set(body.result_ids))!=len(body.result_ids):fail(422,'VALIDATION_FAILED','결과 행 중복입니다.')
        runs={one(c,'analysis_result',r)['run_id'] for r in body.result_ids}
        if len(runs)!=1:fail(422,'VALIDATION_FAILED','도표는 하나의 분석 실행을 사용해야 합니다.')
        version=max([r['version_no'] for r in rows(c,'chart',chart_key=body.chart_key)],default=0)+1
        cid=add(c,'chart',chart_key=body.chart_key,version_no=version,title=body.title,chart_spec=body.chart_spec,created_by=a['user_id'])
        for i,rid in enumerate(body.result_ids):add(c,'chart_result',chart_id=cid,result_id=rid,display_order=i)
        return response(one(c,'chart',cid),request,201,{'Location':f'/api/v1/admin/charts/{cid}'})
    @app.get('/api/v1/admin/charts/{chart_id}')
    def chart_detail(chart_id:int,request:Request):
        c=request.state.c;r=one(c,'chart',chart_id);r['result_ids']=[x['result_id'] for x in rows(c,'chart_result',chart_id=chart_id)];return response(r,request)
    @app.post('/api/v1/admin/evidence/searches')
    def search(body:SearchInput,request:Request):
        a=actor(request,'SEARCHER','REVIEWER','EDITOR');c=request.state.c;start=time.monotonic();hits=[]
        if set(body.filters)-{'source_ids'}:fail(422,'UNSUPPORTED_FILTER','초기 검색은 source_ids 필터와 본문 키워드를 지원합니다.')
        words=body.query.casefold().split()
        for ev in rows(c,'evidence_chunk'):
            f=one(c,'source_file',ev['file_id']);src=one(c,'source_document',f['source_id'])
            if body.filters.get('source_ids') and f['source_id'] not in list(map(int,body.filters['source_ids'])):continue
            score=sum(w in (ev['content']+' '+src['title']).casefold() for w in words)
            if score:hits.append({'evidence':{**ev,'source':src},'rank':score})
        hits=sorted(hits,key=lambda h:-h['rank'])[:body.limit];elapsed=int((time.monotonic()-start)*1000)
        sid=add(c,'search_log',actor_id=a['user_id'],query=body.query,filters=body.filters,result_refs=[h['evidence']['evidence_id'] for h in hits],elapsed_ms=elapsed,status='SUCCESS' if hits else 'EMPTY',usage={'index':'keyword-v1','calls':0},searched_at=now())
        return response({'search_id':sid,'hits':hits,'elapsed_ms':elapsed,'usage':{'index':'keyword-v1'}},request)
    @app.get('/api/v1/admin/evidence/{evidence_id}')
    def evidence_detail(evidence_id:int,request:Request):
        c=request.state.c;r=one(c,'evidence_chunk',evidence_id);r['source']=one(c,'source_document',one(c,'source_file',r['file_id'])['source_id']);return response(r,request)

    @app.post('/api/v1/admin/articles',status_code=201)
    def article_create(body:ArticleInput,request:Request):
        a=actor(request,'EDITOR','SEARCHER');c=request.state.c;aid=add(c,'article',slug=body.slug,created_by=a['user_id'],created_at=now())
        out=one(c,'article',aid);out['etag']=etag(c,'article',aid);return response(out,request,201,{'ETag':out['etag'],'Location':f'/api/v1/admin/articles/{aid}/revisions'})
    @app.post('/api/v1/admin/articles/{article_id}/draft-jobs',status_code=202)
    def draft(article_id:int,body:DraftInput,request:Request):
        actor(request,'EDITOR','SEARCHER');precondition(request,'article',article_id)
        if not approved(request.state.c,'ANALYSIS_RUN',body.run_id):fail(409,'APPROVAL_REQUIRED','승인 분석 결과를 사용하세요.')
        for eid in body.evidence_ids:one(request.state.c,'evidence_chunk',eid)
        return jobs(request,'ARTICLE_DRAFT',{'article_id':article_id,**body.model_dump()})
    @app.get('/api/v1/admin/articles/{article_id}/revisions')
    def article_revisions(article_id:int,request:Request):
        c=request.state.c;one(c,'article',article_id);data=rows(c,'article_revision',article_id=article_id)
        return collection([{**r,'body':json.loads(r['body']),'article_etag':etag(c,'article',article_id)} for r in reversed(data)],request)
    @app.post('/api/v1/admin/articles/{article_id}/revisions',status_code=201)
    def revision_create(article_id:int,body:RevisionInput,request:Request):
        a=actor(request,'EDITOR','SEARCHER');precondition(request,'article',article_id);c=request.state.c
        versions=rows(c,'article_revision',article_id=article_id);latest=max(versions,key=lambda r:r['version_no']) if versions else None
        if body.base_revision_id!=(latest['revision_id'] if latest else None):fail(412,'VERSION_CONFLICT','기준 기사 개정본이 다릅니다.')
        blocks={b.block_id:b for b in body.body.blocks}
        if len(blocks)!=len(body.body.blocks) or sum(len(b.text or '') for b in blocks.values())>100000:fail(422,'VALIDATION_FAILED','본문 길이·블록 ID를 확인하세요.')
        for b in blocks.values():
            if b.type=='CHART':
                if b.chart_id is None or b.text is not None:fail(422,'VALIDATION_FAILED','도표 블록 필드를 확인하세요.')
                one(c,'chart',b.chart_id)
            elif b.text is None or b.chart_id is not None:fail(422,'VALIDATION_FAILED','텍스트 블록 필드를 확인하세요.')
        if len({x.claim_no for x in body.claims})!=len(body.claims):fail(422,'VALIDATION_FAILED','주장 순번 중복입니다.')
        for cl in body.claims:
            try:
                bid,start,end=cl.body_locator.rsplit(':',2);start=int(start);end=int(end)
                if start<0 or end<=start or end>len(blocks[bid].text or '') or (blocks[bid].text or '')[start:end]!=cl.text:raise ValueError()
            except (ValueError,KeyError):fail(422,'VALIDATION_FAILED','주장 문장과 본문 위치가 일치하지 않습니다.')
            if cl.is_numeric and not cl.evidence:fail(422,'VALIDATION_FAILED','수치 근거가 필요합니다.')
            for link in cl.evidence:
                if (link.evidence_id is None)==(link.result_id is None):fail(422,'VALIDATION_FAILED','근거 ID 중 하나만 지정하세요.')
                one(c,'evidence_chunk' if link.evidence_id else 'analysis_result',link.evidence_id or link.result_id)
        rid=add(c,'article_revision',article_id=article_id,version_no=(latest['version_no'] if latest else 0)+1,title=body.title,body=json.dumps(body.body.model_dump(exclude_none=True),ensure_ascii=False),created_by=a['user_id'],created_at=now(),change_note=body.change_note)
        for cl in body.claims:
            cid=add(c,'claim',revision_id=rid,**cl.model_dump(exclude={'evidence'}))
            for link in cl.evidence:add(c,'claim_evidence',claim_id=cid,**link.model_dump())
        bump(c,'article',article_id);return response({'revision_id':rid,'article_id':article_id,'article_etag':etag(c,'article',article_id),'claims':rows(c,'claim',revision_id=rid)},request,201,{'Location':f'/api/v1/admin/revisions/{rid}'})
    @app.get('/api/v1/admin/revisions/{revision_id}')
    def revision_detail(revision_id:int,request:Request):
        c=request.state.c;r=one(c,'article_revision',revision_id);r['body']=json.loads(r['body']);r['claims']=rows(c,'claim',revision_id=revision_id);tag=etag(c,'article_revision',revision_id)
        for cl in r['claims']:cl['evidence']=rows(c,'claim_evidence',claim_id=cl['claim_id'])
        r['etag']=tag;return response(r,request,headers={'ETag':tag})
    @app.post('/api/v1/admin/revisions/{revision_id}/checks',status_code=202)
    def check_revision(revision_id:int,body:CheckInput,request:Request):
        actor(request,'SEARCHER','REVIEWER','EDITOR');precondition(request,'article_revision',revision_id)
        return jobs(request,'RULE_CHECK',{'target_table':'article_revision','target_id':revision_id,**body.model_dump()})
    @app.get('/api/v1/admin/revisions/{revision_id}/checks')
    def checks_result(revision_id:int,request:Request,rule_version:str):return collection(rows(request.state.c,'rule_check',revision_id=revision_id,rule_version=rule_version),request)
    @app.get('/api/v1/admin/fact-checks')
    def fact_list(request:Request,fc_code:str|None=None):
        c=request.state.c;data=rows(c,'fact_check',**({'fc_code':fc_code} if fc_code else {}))
        return collection([{**f,'claim_checks':[{**r,'effective_approval':approval(c,'FACT_CHECK_REVISION',r['fc_revision_id'])} for r in rows(c,'fact_check_revision',fc_id=f['fc_id'])],'etag':etag(c,'fact_check',f['fc_id'])} for f in data],request)
    @app.post('/api/v1/admin/fact-checks/{fc_code}/revisions',status_code=201)
    def fact_revision(fc_code:str,body:FactInput,request:Request):
        a=actor(request,'REVIEWER');c=request.state.c;fs=rows(c,'fact_check',fc_code=fc_code)
        if not fs:fail(404,'NOT_FOUND','FC01~FC20 항목을 사용하세요.')
        fc=fs[0];precondition(request,'fact_check',fc['fc_id']);claim=one(c,'claim',body.claim_id)
        for p in rows(c,'publication',status='PUBLISHED'):
            if claim['revision_id'] in {x['revision_id'] for x in rows(c,'publication_item',publication_id=p['publication_id'])}:
                fail(409,'CLAIM_ALREADY_PUBLISHED','공개된 주장 검증은 새 기사 개정본에서 진행하세요. 기존 오류는 총괄의 승인 철회로 기록합니다.')
        version=max([x['version_no'] for x in rows(c,'fact_check_revision',fc_id=fc['fc_id'])],default=0)+1
        rid=add(c,'fact_check_revision',fc_id=fc['fc_id'],version_no=version,checked_by=a['user_id'],checked_at=now(),**body.model_dump())
        bump(c,'fact_check',fc['fc_id']);return response(one(c,'fact_check_revision',rid),request,201,{'Location':f'/api/v1/admin/review-targets/FACT_CHECK_REVISION/{rid}'})
    @app.get('/api/v1/admin/review-targets/{target_type}/{target_id}')
    def review_detail(target_type:str,target_id:int,request:Request):
        c=request.state.c;r=target(c,target_type,target_id);tag=etag(c,TARGETS[target_type][0],target_id)
        return response({'target_type':target_type,'target_id':target_id,'author_ids':[r[TARGETS[target_type][2]]],'effective_approval':approval(c,target_type,target_id),'blocking_issues':blocking(c,target_type,target_id),'etag':tag},request,headers={'ETag':tag})
    def impact_ids(c,kind,pk):
        for p in rows(c,'publication'):
            items=rows(c,'publication_item',publication_id=p['publication_id'])
            revisionids={i['revision_id'] for i in items if i['revision_id']}
            claims=[cl for rid in revisionids for cl in rows(c,'claim',revision_id=rid)]
            runids=set()
            for cl in claims:
                for link in rows(c,'claim_evidence',claim_id=cl['claim_id']):
                    if link['result_id']:runids.add(one(c,'analysis_result',link['result_id'])['run_id'])
            for item in items:
                if item['chart_id']:
                    for link in rows(c,'chart_result',chart_id=item['chart_id']):runids.add(one(c,'analysis_result',link['result_id'])['run_id'])
            match=(kind=='PUBLICATION' and pk==p['publication_id']) or (kind=='ARTICLE_REVISION' and pk in revisionids) or (kind=='ANALYSIS_RUN' and pk in runids) or (kind=='DATASET' and pk in {one(c,'analysis_run',r)['dataset_id'] for r in runids}) or (kind=='FACT_CHECK_REVISION' and one(c,'fact_check_revision',pk)['claim_id'] in {x['claim_id'] for x in claims})
            if match:yield p
    @app.post('/api/v1/admin/review-targets/{target_type}/{target_id}/decisions',status_code=201)
    def decision(target_type:str,target_id:int,body:DecisionInput,request:Request):
        c=request.state.c;a=actor(request);r=review_auth(c,a,target_type,target_id);precondition(request,TARGETS[target_type][0],target_id)
        if target_type in ('DATASET','PUBLICATION'):busy(c,TARGETS[target_type][0],target_id)
        live=[p for p in impact_ids(c,target_type,target_id) if p['status']=='PUBLISHED']
        if live and body.decision!='APPROVED':
            require(a,'LEAD')
            if body.decision!='REVOKED':fail(409,'INVALID_STATE','공개 중인 승인은 철회 절차를 사용하세요.')
            for p in live:update(c,'publication',p['publication_id'],status='WITHDRAWN',correction_note=body.review_note)
        if body.decision=='APPROVED':
            if target_type=='PUBLICATION' and r['status']!='FROZEN':fail(409,'INVALID_STATE','공개 묶음을 먼저 고정하세요.')
            enforce_ready(c,target_type,target_id)
        fields={key:None for _,key,_ in TARGETS.values()};fields[TARGETS[target_type][1]]=target_id
        aid=add(c,'approval',**fields,reviewer_id=a['user_id'],decision=body.decision,review_note=body.review_note,decided_at=now(),snapshot_token=r.get('revision_token',1))
        return response(one(c,'approval',aid),request,201)
    @app.get('/api/v1/admin/review-targets/{target_type}/{target_id}/impact')
    def impact(target_type:str,target_id:int,request:Request):
        target(request.state.c,target_type,target_id);return collection([{'entity_type':'PUBLICATION','entity_id':p['publication_id'],'status':p['status'],'action_required':'새 버전 재검토 또는 공개 철회'} for p in impact_ids(request.state.c,target_type,target_id)],request)

    def items_create(c,pid,items):
        seen=set()
        for i,item in enumerate(items):
            if (item.revision_id is None)==(item.chart_id is None):fail(422,'VALIDATION_FAILED','기사 또는 도표 중 하나를 지정하세요.')
            key=('R',item.revision_id) if item.revision_id else ('C',item.chart_id)
            if key in seen:fail(422,'VALIDATION_FAILED','구성 항목이 중복됩니다.')
            seen.add(key);one(c,'article_revision' if item.revision_id else 'chart',item.revision_id or item.chart_id)
            add(c,'publication_item',publication_id=pid,display_order=i,**item.model_dump())
    @app.post('/api/v1/admin/publications',status_code=201)
    def pub_create(body:PublicationInput,request:Request):
        a=actor(request,'EDITOR','LEAD');c=request.state.c
        p=add(c,'publication',version_no=max([x['version_no'] for x in rows(c,'publication')],default=0)+1,status='DRAFT',created_by=a['user_id'],created_at=now(),published_at=None,correction_note=body.correction_note,report_uri=None,journal_uri=None)
        items_create(c,p,body.item_refs);return response(one(c,'publication',p),request,201,{'Location':f'/api/v1/admin/publications/{p}'})
    @app.get('/api/v1/admin/publications/{publication_id}')
    def pub_detail(publication_id:int,request:Request):
        c=request.state.c;r=one(c,'publication',publication_id);r.update(items=rows(c,'publication_item',publication_id=publication_id),files=rows(c,'handover_file',publication_id=publication_id),blocking_issues=blocking(c,'PUBLICATION',publication_id),etag=etag(c,'publication',publication_id))
        return response(r,request,headers={'ETag':r['etag']})
    @app.put('/api/v1/admin/publications/{publication_id}/items')
    def pub_items(publication_id:int,body:ItemsInput,request:Request):
        actor(request,'EDITOR');protect(request,'publication',publication_id);c=request.state.c
        c.execute(T['publication_item'].delete().where(T['publication_item'].c.publication_id==publication_id));items_create(c,publication_id,body.items);bump(c,'publication',publication_id)
        return details(request,'publication',publication_id)
    @app.post('/api/v1/admin/publications/{publication_id}/files',status_code=201)
    async def pub_file(publication_id:int,request:Request,category:str=Form(...),access_scope:str=Form(...),file:UploadFile=File(...)):
        actor(request,'EDITOR');protect(request,'publication',publication_id);c=request.state.c
        if category not in ('RAW','CLEAN','CODE','EVIDENCE','CHECK','APPROVAL','FINAL') or access_scope not in ('PUBLIC','INTERNAL'):fail(422,'VALIDATION_FAILED','파일 분류·이용 범위를 확인하세요.')
        extension=Path(file.filename or '').suffix.lower()
        if extension not in ('.csv','.txt','.md','.json','.py','.sql','.pdf','.docx'):fail(415,'UNSUPPORTED_MEDIA_TYPE','허용 파일 형식이 아닙니다.')
        data=await file.read(50*1024*1024+1)
        if len(data)>50*1024*1024:fail(413,'PAYLOAD_TOO_LARGE','50MiB 한도입니다.')
        if category=='FINAL':
            if extension not in ('.pdf','.docx'):fail(415,'UNSUPPORTED_MEDIA_TYPE','최종본은 PDF 또는 DOCX여야 합니다.')
            if extension=='.pdf' and not data.startswith(b'%PDF-'):fail(415,'UNSUPPORTED_MEDIA_TYPE','PDF 서명을 확인하세요.')
            if extension=='.docx':
                import zipfile
                try:
                    z=zipfile.ZipFile(io.BytesIO(data))
                    if 'word/document.xml' not in z.namelist() or any(i.file_size>50*1024*1024 for i in z.infolist()) or any('vbaProject' in n for n in z.namelist()):raise ValueError()
                except Exception:fail(415,'UNSUPPORTED_MEDIA_TYPE','안전한 DOCX 구조가 아닙니다.')
        uri,digest=save_blob(data);fid=add(c,'handover_file',publication_id=publication_id,category=category,storage_uri=uri,sha256=digest,access_scope=access_scope,verified_at=None);bump(c,'publication',publication_id)
        return response(one(c,'handover_file',fid),request,201)
    @app.post('/api/v1/admin/publications/{publication_id}/file-verifications',status_code=202)
    def verify_files(publication_id:int,body:FileCheckInput,request:Request):
        actor(request,'REVIEWER','EDITOR');r=protect(request,'publication',publication_id)
        return jobs(request,'FILE_VERIFICATION',{'target_table':'publication','target_id':publication_id,'token':r['revision_token'],**body.model_dump()})
    @app.post('/api/v1/admin/publications/{publication_id}/freeze')
    def pub_freeze(publication_id:int,body:FreezeInput,request:Request):
        actor(request,'EDITOR','LEAD');protect(request,'publication',publication_id);c=request.state.c
        if body.report_file_id==body.journal_file_id:fail(422,'VALIDATION_FAILED','리포트와 뉴스저널은 다른 파일이어야 합니다.')
        for fid in (body.report_file_id,body.journal_file_id):
            f=one(c,'handover_file',fid)
            if f['publication_id']!=publication_id or f['category']!='FINAL' or not f['verified_at']:fail(422,'VALIDATION_FAILED','같은 묶음의 검수한 FINAL 파일을 지정하세요.')
        update(c,'publication',publication_id,report_file_id=body.report_file_id,journal_file_id=body.journal_file_id,report_uri=one(c,'handover_file',body.report_file_id)['storage_uri'],journal_uri=one(c,'handover_file',body.journal_file_id)['storage_uri'])
        enforce_ready(c,'PUBLICATION',publication_id);update(c,'publication',publication_id,status='FROZEN');return details(request,'publication',publication_id)
    @app.post('/api/v1/admin/publications/{publication_id}/publish')
    def publish(publication_id:int,request:Request):
        actor(request,'LEAD');precondition(request,'publication',publication_id);c=request.state.c;p=one(c,'publication',publication_id)
        if p['status']!='FROZEN' or not approved(c,'PUBLICATION',publication_id):fail(409,'APPROVAL_REQUIRED','고정된 묶음의 독립 승인이 필요합니다.')
        enforce_ready(c,'PUBLICATION',publication_id);update(c,'publication',publication_id,status='PUBLISHED',published_at=now());return details(request,'publication',publication_id)
    @app.post('/api/v1/admin/publications/{publication_id}/withdraw')
    def withdraw(publication_id:int,body:WithdrawInput,request:Request):
        actor(request,'LEAD');precondition(request,'publication',publication_id);c=request.state.c
        if one(c,'publication',publication_id)['status']!='PUBLISHED':fail(409,'INVALID_STATE','공개 중인 묶음만 철회할 수 있습니다.')
        update(c,'publication',publication_id,status='WITHDRAWN',correction_note=body.correction_note);return details(request,'publication',publication_id)
    @app.get('/api/v1/admin/jobs/{job_id}')
    def job_get(job_id:str,request:Request):
        a=actor(request);job=one(request.state.c,'api_job',job_id)
        if job['actor_id']!=a['user_id'] and 'LEAD' not in a['roles']:fail(403,'FORBIDDEN','다른 사용자의 작업입니다.')
        job.pop('payload',None);return response(job,request)
    @app.get('/api/v1/admin/audit-events')
    def audit_list(request:Request,entity_type:str|None=None,entity_key:str|None=None):
        actor(request,'REVIEWER','LEAD');return collection(list(reversed(rows(request.state.c,'audit_event',**{k:v for k,v in {'entity_type':entity_type,'entity_key':entity_key}.items() if v is not None}))),request)
    @app.get('/api/v1/admin/users/{user_id}')
    def user_get(user_id:int,request:Request):
        actor(request,'LEAD');c=request.state.c;r=one(c,'app_user',user_id);r['roles']=[x['role_code'] for x in rows(c,'user_role',user_id=user_id)];r['etag']=etag(c,'app_user',user_id);return response(r,request,headers={'ETag':r['etag']})
    @app.put('/api/v1/admin/users/{user_id}/roles')
    def user_roles(user_id:int,body:RolesInput,request:Request):
        a=actor(request,'LEAD');precondition(request,'app_user',user_id);c=request.state.c;one(c,'app_user',user_id)
        leads=[x['user_id'] for x in rows(c,'user_role',role_code='LEAD') if one(c,'app_user',x['user_id'])['is_active']]
        if user_id in leads and len(leads)==1 and 'LEAD' not in body.roles:fail(409,'LAST_LEAD','마지막 활성 총괄을 제거할 수 없습니다.')
        c.execute(T['user_role'].delete().where(T['user_role'].c.user_id==user_id))
        for role in set(body.roles):add(c,'user_role',user_id=user_id,role_code=role,granted_by=a['user_id'],granted_at=now())
        bump(c,'app_user',user_id);return response({'user_id':user_id,'roles':body.roles,'etag':etag(c,'app_user',user_id)},request)
    app.mount('/static',StaticFiles(directory=Path(__file__).with_name('static')),name='static')
    @app.get('/',include_in_schema=False)
    def index():return FileResponse(Path(__file__).with_name('static')/'index.html')
    original_openapi=app.openapi
    def secured_openapi():
        spec=original_openapi()
        spec.setdefault('components',{}).setdefault('securitySchemes',{})['BearerAuth']={'type':'http','scheme':'bearer','description':'CLI 발급 12시간 토큰. 운영 OIDC는 미연동.'}
        for path,methods in spec['paths'].items():
            if path.startswith('/api/v1/admin/'):
                for method,operation in methods.items():
                    if method in ('get','post','put','patch','delete'):operation['security']=[{'BearerAuth':[]}]
                    if method in ('post','put','patch'):
                        params=operation.setdefault('parameters',[])
                        if not any(p.get('name')=='Idempotency-Key' for p in params):params.append({'name':'Idempotency-Key','in':'header','required':True,'schema':{'type':'string','format':'uuid'}})
                        conditional=method in ('put','patch') or path.endswith(('/imports','/quality-checks','/freeze','/decisions','/checks','/draft-jobs','/files','/file-verifications','/publish','/withdraw','/revisions'))
                        if conditional and not any(p.get('name')=='If-Match' for p in params):params.append({'name':'If-Match','in':'header','required':True,'schema':{'type':'string'},'description':'대상 상세 조회의 ETag'})
        return spec
    app.openapi=secured_openapi
    return app

app=create_app()
