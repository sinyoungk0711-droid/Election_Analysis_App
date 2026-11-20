"""Transactional business rules shared by HTTP handlers and background workers."""
import hashlib
import json
import secrets
import uuid
from datetime import timedelta, timezone
from pathlib import Path
from decimal import Decimal

import sqlalchemy as sa
from fastapi import HTTPException
from .db import T, DATA, now, one, rows, add, update, serialize

TARGETS={'DATASET':('dataset_version','dataset_id','created_by'),
 'ANALYSIS_RUN':('analysis_run','run_id','executed_by'),
 'ARTICLE_REVISION':('article_revision','revision_id','created_by'),
 'FACT_CHECK_REVISION':('fact_check_revision','fc_revision_id','checked_by'),
 'PUBLICATION':('publication','publication_id','created_by')}
ROLES={'ANALYST','SEARCHER','REVIEWER','EDITOR','LEAD'}

def fail(status,code,message,**extra):
    raise HTTPException(status,{'code':code,'detail':message,**extra})

def require(actor,*allowed):
    if not set(actor['roles']).intersection(allowed): fail(403,'FORBIDDEN','이 작업을 수행할 권한이 없습니다.')

def audit(c,actor,entity,key,action,detail):
    add(c,'audit_event',actor_id=actor['user_id'] if actor else None,entity_type=entity,
        entity_key=str(key),action=action,before_after=serialize(detail),occurred_at=now())

def issue_token(c,user_id):
    token=secrets.token_urlsafe(36)
    add(c,'access_token',token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user_id,expires_at=now()+timedelta(hours=12))
    return token

def authenticate(c,token):
    table=T['access_token']
    tokenrow=c.execute(sa.select(table).where(table.c.token_hash==hashlib.sha256(token.encode()).hexdigest())).mappings().first()
    if not tokenrow or tokenrow['expires_at'].replace(tzinfo=timezone.utc)<now(): fail(401,'UNAUTHENTICATED','인증 토큰이 없거나 만료되었습니다.')
    actor=one(c,'app_user',tokenrow['user_id'])
    if not actor['is_active']: fail(403,'ACCOUNT_DISABLED','비활성 계정입니다.')
    actor['roles']=[r['role_code'] for r in rows(c,'user_role',user_id=actor['user_id'])]
    return actor

def bump(c,table,pk):
    row=one(c,table,pk); update(c,table,pk,revision_token=row['revision_token']+1)

def target(c,kind,pk):
    if kind not in TARGETS: fail(422,'VALIDATION_FAILED','잘못된 승인 대상 종류입니다.')
    name,key,author=TARGETS[kind]
    return one(c,name,pk)

def approval(c,kind,pk):
    name,key,author=TARGETS[kind]
    items=rows(c,'approval',**{key:pk})
    return max(items,key=lambda r:r['approval_id']) if items else None

def approved(c,kind,pk):
    row=target(c,kind,pk); a=approval(c,kind,pk)
    return bool(a and a['decision']=='APPROVED' and a['snapshot_token']==row.get('revision_token',1))

def current_fact_checks(c,claim_id):
    latest={}
    for check in rows(c,'fact_check_revision',claim_id=claim_id):
        prev=latest.get(check['fc_id'])
        if prev is None or check['version_no']>prev['version_no']:latest[check['fc_id']]=check
    return list(latest.values())

def etag(c,table,pk):
    row=one(c,table,pk)
    kind=next((k for k,v in TARGETS.items() if v[0]==table),None)
    snapshot={'row':serialize(row),'approval':serialize(approval(c,kind,pk)) if kind else None}
    digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    return '"'+digest+'"'

def precondition(request,table,pk):
    expected=request.headers.get('if-match')
    if not expected: fail(428,'PRECONDITION_REQUIRED','최신 ETag를 If-Match 헤더로 보내주세요.')
    if expected!=etag(request.state.c,table,pk): fail(412,'VERSION_CONFLICT','다른 작업으로 내용이 바뀌었습니다. 최신 상태를 확인하세요.')

def busy(c,table,pk):
    for j in rows(c,'api_job'):
        if j['status'] in ('QUEUED','RUNNING') and j['payload'].get('target_table')==table and j['payload'].get('target_id')==pk:
            fail(409,'RESOURCE_BUSY','이 작업본을 처리하는 작업이 진행 중입니다.')

def mutable(c,table,pk):
    row=one(c,table,pk)
    if row['status']!='DRAFT': fail(409,'IMMUTABLE_RESOURCE','고정된 자료는 수정할 수 없습니다. 새 버전을 만드세요.')
    busy(c,table,pk)
    return row

def enqueue(c,actor,kind,payload):
    job_id=str(uuid.uuid4())
    add(c,'api_job',job_id=job_id,actor_id=actor['user_id'],type=kind,status='QUEUED',payload=payload,progress=0,created_at=now(),attempts=0)
    return one(c,'api_job',job_id)

def blocking(c,kind,pk):
    row=target(c,kind,pk); issues=[]
    def block(code,msg,typ=kind,tid=pk):
        issues.append({'code':code,'message':msg,'target_type':typ,'target_id':tid})
    if kind=='DATASET':
        observations=rows(c,'turnout_observation',dataset_id=pk)
        if not observations: block('NO_DATA','투표 관측이 없습니다.')
        if any(i['status']=='OPEN' for i in rows(c,'quality_issue',dataset_id=pk)): block('QUALITY_BLOCKED','미해결 품질 이슈가 있습니다.')
        completions=rows(c,'quality_issue',dataset_id=pk,rule_code='CHECK_COMPLETE')
        if not completions or max(completions,key=lambda x:x['issue_id'])['details'].get('content_token')!=row['revision_token']: block('CHECK_REQUIRED','현재 내용 버전에 대해 품질 검사를 실행해야 합니다.')
        members=rows(c,'comparison_member',dataset_id=pk)
        included=[r['region_id'] for r in members if r['is_included']]
        if not included: block('NO_COMPARISON_SET','공통 비교 지역을 설정하세요.')
        for region_id in included:
            if len({r['election_id'] for r in observations if r['region_id']==region_id})!=3: block('INCOMPLETE_REGION','포함 지역의 세 선거 관측이 필요합니다.','REGION',region_id)
        for obs in observations:
            lineages=rows(c,'observation_lineage',observation_id=obs['observation_id'])
            if not lineages: block('MISSING_LINEAGE','원본 행 연결이 없습니다.','OBSERVATION',obs['observation_id'])
            for link in lineages:
                mapping=one(c,'region_mapping',link['mapping_id'])
                if any(mapping[k]!=obs[k] for k in ('dataset_id','election_id','region_id')): block('LINEAGE_MISMATCH','원본 대응 관계가 불일치합니다.')
    if kind=='ANALYSIS_RUN':
        if row['status']!='SUCCEEDED': block('RUN_NOT_READY','분석이 완료되지 않았습니다.')
        if not approved(c,'DATASET',row['dataset_id']): block('DATASET_NOT_APPROVED','데이터 승인이 유효하지 않습니다.')
    if kind=='FACT_CHECK_REVISION':
        claim=one(c,'claim',row['claim_id'])
        if not rows(c,'claim_evidence',claim_id=claim['claim_id']): block('MISSING_EVIDENCE','주장에 근거를 연결하세요.')
        if row['verdict']!='SUPPORTED': block('CLAIM_NOT_SUPPORTED','확인되지 않거나 수정이 필요한 주장입니다.')
    if kind=='ARTICLE_REVISION':
        claims=rows(c,'claim',revision_id=pk)
        if not claims: block('CLAIMS_REQUIRED','검증할 주장을 등록하세요.')
        for claim in claims:
            links=rows(c,'claim_evidence',claim_id=claim['claim_id'])
            if claim['is_numeric'] and not links: block('MISSING_EVIDENCE','수치 주장 근거가 없습니다.','CLAIM',claim['claim_id'])
            if any(x['relation_type']=='CONTRADICT' for x in links): block('CONTRADICTING_EVIDENCE','상충하는 근거를 해결하세요.','CLAIM',claim['claim_id'])
            for link in links:
                if link['result_id']:
                    result=one(c,'analysis_result',link['result_id'])
                    if not approved(c,'ANALYSIS_RUN',result['run_id']): block('ANALYSIS_NOT_APPROVED','분석 승인 누락입니다.','ANALYSIS_RUN',result['run_id'])
            checks=current_fact_checks(c,claim['claim_id'])
            if any(x['verdict']!='SUPPORTED' for x in checks):block('CLAIM_NEEDS_CORRECTION','주장의 최신 검증 판정을 확인하세요.','CLAIM',claim['claim_id'])
            if claim['is_core'] and not any(approved(c,'FACT_CHECK_REVISION',x['fc_revision_id']) and not blocking(c,'FACT_CHECK_REVISION',x['fc_revision_id']) for x in checks):
                block('CORE_CLAIM_NOT_APPROVED','핵심 주장 독립 승인이 필요합니다.','CLAIM',claim['claim_id'])
        checks=rows(c,'rule_check',revision_id=pk)
        for category in ('NUMBER','DATE','TARGET','SOURCE','UNIT'):
            scoped=[x for x in checks if x['category']==category]
            if not scoped or max(scoped,key=lambda x:x['check_id'])['status']!='PASS': block('RULE_CHECK_FAILED',category+' 검사 통과가 필요합니다.')
    if kind=='PUBLICATION':
        items=rows(c,'publication_item',publication_id=pk)
        revisions=[i['revision_id'] for i in items if i['revision_id']]
        chart_ids=[i['chart_id'] for i in items if i['chart_id']]
        if not revisions: block('ARTICLE_REQUIRED','기사가 필요합니다.')
        runs=set()
        for revision in revisions:
            if not approved(c,'ARTICLE_REVISION',revision): block('ARTICLE_NOT_APPROVED','기사 독립 승인 누락입니다.','ARTICLE_REVISION',revision)
            issues.extend(blocking(c,'ARTICLE_REVISION',revision))
            rev=one(c,'article_revision',revision)
            body=json.loads(rev['body'])
            for b in body['blocks']:
                if b['type']=='CHART' and b['chart_id'] not in chart_ids: block('CHART_NOT_INCLUDED','본문 도표를 묶음에 포함하세요.')
            for claim in rows(c,'claim',revision_id=revision):
                for link in rows(c,'claim_evidence',claim_id=claim['claim_id']):
                    if link['result_id']: runs.add(one(c,'analysis_result',link['result_id'])['run_id'])
        for chart in chart_ids:
            for link in rows(c,'chart_result',chart_id=chart): runs.add(one(c,'analysis_result',link['result_id'])['run_id'])
        if len(runs)!=1: block('MIXED_ANALYSIS_VERSION','단일 승인 분석 실행을 사용해야 합니다.')
        for run in runs:
            if not approved(c,'ANALYSIS_RUN',run): block('ANALYSIS_NOT_APPROVED','분석 승인이 유효하지 않습니다.')
        publication_claims={r['claim_id'] for rev in revisions for r in rows(c,'claim',revision_id=rev)}
        for fc in rows(c,'fact_check'):
            checks=[x for cid in publication_claims for x in current_fact_checks(c,cid) if x['fc_id']==fc['fc_id']]
            if not any(approved(c,'FACT_CHECK_REVISION',x['fc_revision_id']) and x['verdict']=='SUPPORTED' for x in checks): block('FC_NOT_COMPLETE',fc['fc_code']+' 승인 누락입니다.','FACT_CHECK',fc['fc_id'])
        files=rows(c,'handover_file',publication_id=pk)
        for file in files:
            if not file['verified_at']: block('FILE_NOT_VERIFIED','파일 검수가 필요합니다.','HANDOVER_FILE',file['handover_file_id'])
        if not row.get('report_file_id') or not row.get('journal_file_id'): block('FINAL_FILES_REQUIRED','리포트와 뉴스저널을 지정하세요.')
    return issues

def enforce_ready(c,kind,pk):
    issues=blocking(c,kind,pk)
    if issues: fail(409,'APPROVAL_REQUIRED','선행 조건을 완료해야 합니다.',blocking_issues=issues)

def review_auth(c,actor,kind,pk):
    require(actor,'REVIEWER','LEAD')
    row=target(c,kind,pk)
    if kind=='PUBLICATION': require(actor,'LEAD')
    authors={row[TARGETS[kind][2]]}
    if kind=='FACT_CHECK_REVISION':
        claim=one(c,'claim',row['claim_id']); authors.add(one(c,'article_revision',claim['revision_id'])['created_by'])
    if actor['user_id'] in authors: fail(403,'SELF_APPROVAL','작성·검증 당사자는 자기 결과물을 승인할 수 없습니다.')
    return row

def published(c,pk=None):
    if pk is None:
        options=[x for x in rows(c,'publication') if x['status']=='PUBLISHED']
        if not options: fail(404,'NO_PUBLICATION','아직 공개된 보도가 없습니다.')
        return max(options,key=lambda x:x['version_no'])
    row=one(c,'publication',pk)
    if row['status']=='WITHDRAWN': fail(410,'PUBLICATION_WITHDRAWN','철회된 공개본입니다. 정정 안내를 확인하세요.')
    if row['status']!='PUBLISHED': fail(404,'NOT_FOUND','공개 자료가 아닙니다.')
    return row

def save_blob(data):
    digest=hashlib.sha256(data).hexdigest()
    folder=DATA/'files'; folder.mkdir(exist_ok=True)
    path=folder/digest
    if not path.exists(): path.write_bytes(data)
    return str(path),digest

def safe_file(row):
    path=Path(row['storage_uri']).resolve()
    root=(DATA/'files').resolve()
    if not path.is_relative_to(root) or not path.is_file(): fail(404,'FILE_NOT_FOUND','파일을 찾을 수 없습니다.')
    return path

def public_items(c,pid):
    published(c,pid)
    return rows(c,'publication_item',publication_id=pid)

def publication_runs(c,pid):
    run_ids=set()
    for i in public_items(c,pid):
        if i['chart_id']:
            for x in rows(c,'chart_result',chart_id=i['chart_id']): run_ids.add(one(c,'analysis_result',x['result_id'])['run_id'])
        if i['revision_id']:
            for cl in rows(c,'claim',revision_id=i['revision_id']):
                for x in rows(c,'claim_evidence',claim_id=cl['claim_id']):
                    if x['result_id']: run_ids.add(one(c,'analysis_result',x['result_id'])['run_id'])
    return run_ids
