import hashlib
import json
import os
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from app.db import T, create_engine, initialize, rows, one, add, update, DATA
from app.cli import init_reference, create_user, seed_demo, demo_csv
from app.main import create_app
from app.service import blocking, issue_token, now, save_blob
from app.worker import tick

@pytest.fixture(autouse=True)
def isolated_storage(tmp_path,monkeypatch):
    import importlib
    storage=tmp_path/'runtime';storage.mkdir()
    for module in ('app.db','app.service','app.cli','app.main'):
        monkeypatch.setattr(importlib.import_module(module),'DATA',storage)

@pytest.fixture
def system(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'test.db').replace('\\','/'));initialize(engine)
    with engine.begin() as c:
        init_reference(c)
        writer,token=create_user(c,'writer',['ANALYST','EDITOR','SEARCHER'])
        reviewer,review_token=create_user(c,'reviewer',['REVIEWER'])
        lead,lead_token=create_user(c,'lead',['LEAD'])
    client=TestClient(create_app(engine),raise_server_exceptions=False)
    return client,engine,{'writer':token,'reviewer':review_token,'lead':lead_token}

def headers(tokens,who='writer',etag=None,key=None):
    return {'Authorization':'Bearer '+tokens[who],'Idempotency-Key':key or str(uuid.uuid4()),**({'If-Match':etag} if etag else {})}

def req(client,tokens,method,path,body=None,who='writer',etag=None,key=None):
    return client.request(method,'/api/v1/admin'+path,json=body,headers=headers(tokens,who,etag,key))

def get(client,tokens,path,who='writer'):
    r=req(client,tokens,'GET',path,who=who);assert r.status_code==200,r.text;return r

def test_auth_and_errors(system):
    client,engine,tokens=system
    r=client.get('/api/v1/admin/me');assert r.status_code==401
    assert r.headers['content-type'].startswith('application/problem+json')
    assert r.headers['www-authenticate']=='Bearer'
    assert req(client,tokens,'POST','/datasets',{'change_note':'new'},who='lead').status_code==403
    assert req(client,tokens,'POST','/datasets',{'change_note':'new','unknown':1}).status_code==422
    assert client.get('/api/v1/public/context').status_code==404
    assert client.get('/api/v1/demo/accounts').status_code==404
    invalid=client.post('/api/v1/admin/datasets',content='{broken',headers={**headers(tokens),'Content-Type':'application/json'})
    assert invalid.status_code==400

def test_idempotency_and_preconditions(system):
    client,engine,tokens=system;key=str(uuid.uuid4())
    a=req(client,tokens,'POST','/datasets',{'change_note':'new'},key=key);assert a.status_code==201,a.text
    b=req(client,tokens,'POST','/datasets',{'change_note':'new'},key=key);assert b.json()==a.json()
    assert req(client,tokens,'POST','/datasets',{'change_note':'changed'},key=key).status_code==409
    did=a.json()['data']['dataset_id'];path=f'/datasets/{did}/quality-checks'
    assert req(client,tokens,'POST',path,{'rule_version':'v1'}).status_code==428
    assert req(client,tokens,'POST',path,{'rule_version':'v1'},etag='"stale"').status_code==412
    current=get(client,tokens,f'/datasets/{did}').headers['etag']
    r=req(client,tokens,'POST',path,{'rule_version':'v1'},etag=current);assert r.status_code==202,r.text
    with engine.begin() as c:assert len(rows(c,'dataset_version'))==1

def test_multipart_idempotency_ignores_random_boundary(system):
    client,engine,tokens=system
    source=req(client,tokens,'POST','/sources',{'title':'source','publisher':'test','usage_terms':'test'}).json()['data']['source_id']
    key=str(uuid.uuid4());data={'source_id':source,'collection_scope':'sample'}
    a=client.post('/api/v1/admin/source-files',data=data,files={'file':('test.csv',demo_csv(),'text/csv')},headers=headers(tokens,key=key))
    b=client.post('/api/v1/admin/source-files',data=data,files={'file':('test.csv',demo_csv(),'text/csv')},headers=headers(tokens,key=key))
    assert a.status_code==201,a.text
    assert b.json()==a.json()

def test_csv_import_analysis_and_self_approval(system):
    client,engine,tokens=system
    source=req(client,tokens,'POST','/sources',{'title':'synthetic','publisher':'test','usage_terms':'sample'}).json()['data']
    upload=client.post('/api/v1/admin/source-files',data={'source_id':source['source_id'],'collection_scope':'sample'},files={'file':('example.csv',demo_csv(),'text/csv')},headers=headers(tokens));assert upload.status_code==201,upload.text
    ds=req(client,tokens,'POST','/datasets',{'change_note':'test'}).json()['data'];did=ds['dataset_id']
    etag=get(client,tokens,f'/datasets/{did}').headers['etag']
    j=req(client,tokens,'POST',f'/datasets/{did}/imports',{'file_ids':[upload.json()['data']['file_id']]},etag=etag);assert j.status_code==202,j.text
    assert tick(engine)
    job=get(client,tokens,'/jobs/'+j.json()['data']['job_id']).json()['data'];assert job['status']=='SUCCEEDED',job
    observations=get(client,tokens,f'/datasets/{did}/observations').json()['data'];assert len(observations)==9
    assert isinstance(observations[0]['voters'],str)
    etag=get(client,tokens,f'/datasets/{did}').headers['etag']
    j=req(client,tokens,'POST',f'/datasets/{did}/quality-checks',{'rule_version':'v1'},etag=etag);assert j.status_code==202
    assert req(client,tokens,'POST',f'/datasets/{did}/freeze',{'change_note':'early'},etag=etag).status_code==409
    tick(engine)
    target=f'/review-targets/DATASET/{did}';tag=get(client,tokens,target).headers['etag']
    own=req(client,tokens,'POST',target+'/decisions',{'decision':'APPROVED','review_note':'self'},etag=tag);assert own.status_code==403
    r=req(client,tokens,'POST',target+'/decisions',{'decision':'APPROVED','review_note':'independent'},who='reviewer',etag=tag);assert r.status_code==201,r.text
    tag=get(client,tokens,f'/datasets/{did}').headers['etag']
    r=req(client,tokens,'POST',f'/datasets/{did}/freeze',{'change_note':'complete'},etag=tag);assert r.status_code==200,r.text
    assert req(client,tokens,'POST',f'/datasets/{did}/quality-checks',{'rule_version':'new'},etag=r.headers['etag']).status_code==409
    r=req(client,tokens,'POST','/analysis-runs',{'dataset_id':did});assert r.status_code==202,r.text;tick(engine)
    job=get(client,tokens,'/jobs/'+r.json()['data']['job_id']).json()['data'];assert job['status']=='SUCCEEDED',job
    run_id=job['result']['resource_id'];values=get(client,tokens,f'/analysis-runs/{run_id}/results?limit=100').json()['data']
    with engine.begin() as c:
        national=[r for r in rows(c,'analysis_result',run_id=int(run_id)) if r['region_id'] is None and one(c,'metric_definition',r['metric_id'])['metric_code']=='TURNOUT']
        assert [r['value'] for r in national]==[Decimal(60),Decimal(66),Decimal(67)]
    assert isinstance(values[0]['value'],str)

def test_bad_csv_fails_atomically(system):
    client,engine,tokens=system
    with engine.begin() as c:
        source=add(c,'source_document',title='bad',publisher='test',published_on=None,source_url=None,usage_terms='test')
        data=demo_csv().replace(b'60000',b'999999',1);uri,digest=save_blob(data)
        file=add(c,'source_file',source_id=source,storage_uri=uri,sha256=digest,collected_at=now(),collection_scope='test')
    ds=req(client,tokens,'POST','/datasets',{'change_note':'bad'}).json()['data'];did=ds['dataset_id'];tag=get(client,tokens,f'/datasets/{did}').headers['etag']
    j=req(client,tokens,'POST',f'/datasets/{did}/imports',{'file_ids':[file]},etag=tag).json()['data'];tick(engine)
    assert get(client,tokens,'/jobs/'+j['job_id']).json()['data']['status']=='FAILED'
    with engine.begin() as c:assert not rows(c,'turnout_observation',dataset_id=int(did))

@pytest.fixture
def demo(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'demo.db').replace('\\','/'));initialize(engine)
    accounts=seed_demo(engine)
    tokens=dict(zip(['writer','reviewer','lead'],[a['token'] for a in accounts]))
    return TestClient(create_app(engine),raise_server_exceptions=False),engine,tokens

def test_complete_public_trace_and_withdraw(demo):
    client,engine,tokens=demo
    ctx=client.get('/api/v1/public/context');assert ctx.status_code==200,ctx.text
    pid=ctx.json()['data']['publication_id'];q='?publication_id='+pid
    for path in ['/summary','/elections','/regions','/articles','/fact-checks','/methodology','/files']:
        r=client.get('/api/v1/public'+path+q);assert r.status_code==200,(path,r.text)
    fcs=client.get('/api/v1/public/fact-checks'+q).json()['data'];assert len(fcs)==20
    cid=fcs[0]['claim_checks'][0]['claim_id']
    assert client.get(f'/api/v1/public/claims/{cid}/evidence'+q).json()['data'][0]['kind']=='RESULT'
    for code,years in [('TURNOUT',[2024]),('CHANGE',[2016,2024]),('REPEATED_LOW',[2016,2020,2024]),('P90_P10',[2024])]:
        r=client.get('/api/v1/public/comparisons'+q+'&metric_code='+code+''.join('&years='+str(y) for y in years));assert r.status_code==200,r.text
    file=client.get('/api/v1/public/files'+q).json()['data'][0]['handover_file_id']
    download=client.get(f'/api/v1/public/files/{file}/download'+q);assert download.content.startswith(b'%PDF-');assert download.headers['cache-control']=='no-store'
    p=get(client,tokens,'/publications/'+pid,who='lead')
    result=req(client,tokens,'POST',f'/publications/{pid}/withdraw',{'correction_note':'test correction'},who='lead',etag=p.headers['etag']);assert result.status_code==200,result.text
    assert client.get('/api/v1/public/summary'+q).status_code==410
    assert client.get(f'/api/v1/public/files/{file}/download'+q).status_code==410
    assert client.get('/api/v1/public/corrections').json()['data'][0]['correction_note']=='test correction'

def test_approval_revocation_withdraws_dependents(demo):
    client,engine,tokens=demo
    r=get(client,tokens,'/review-targets/ANALYSIS_RUN/1',who='lead')
    revoke=req(client,tokens,'POST','/review-targets/ANALYSIS_RUN/1/decisions',{'decision':'REVOKED','review_note':'source changed'},who='lead',etag=r.headers['etag']);assert revoke.status_code==201,revoke.text
    assert client.get('/api/v1/public/summary?publication_id=1').status_code==410

def test_published_fact_check_cannot_be_shadow_replaced(demo):
    client,engine,tokens=demo
    fc=get(client,tokens,'/fact-checks?fc_code=FC01',who='reviewer').json()['data'][0]
    r=req(client,tokens,'POST','/fact-checks/FC01/revisions',{'claim_id':1,'method':'new check','finding':'changed conclusion','verdict':'INCORRECT'},who='reviewer',etag=fc['etag'])
    assert r.status_code==409
    assert r.json()['code']=='CLAIM_ALREADY_PUBLISHED'
    assert client.get('/api/v1/public/fact-checks/FC01?publication_id=1').json()['data']['claim_checks'][0]['verdict']=='SUPPORTED'

def test_unapproved_publication_never_leaks(demo):
    client,engine,tokens=demo
    p=req(client,tokens,'POST','/publications',{'item_refs':[{'revision_id':1}]}).json()['data']
    assert client.get('/api/v1/public/articles?publication_id='+p['publication_id']).status_code==404
    tag=get(client,tokens,'/publications/'+p['publication_id']).headers['etag']
    r=req(client,tokens,'POST','/publications/'+p['publication_id']+'/publish',who='lead',etag=tag);assert r.status_code==409

def test_article_revision_validation_and_immutability(demo):
    client,engine,tokens=demo
    article=req(client,tokens,'POST','/articles',{'slug':'new-test'}).json()['data']
    body={'base_revision_id':None,'title':'example','body':{'blocks':[{'block_id':'p1','type':'PARAGRAPH','text':'가상 전국 투표율은 60.00%다.'}]},'claims':[{'claim_no':1,'text':'가상 전국 투표율은 60.00%다.','body_locator':'p1:0:999','is_core':True,'is_numeric':True,'evidence':[{'result_id':1,'note':'test'}]}],'change_note':'test'}
    # End offsets beyond the string must be rejected rather than accepted by slicing.
    r=req(client,tokens,'POST',f"/articles/{article['article_id']}/revisions",body,etag=article['etag'])
    assert r.status_code==422,r.text
    body['claims'][0]['body_locator']='p1:0:'+str(len(body['claims'][0]['text']))
    r=req(client,tokens,'POST',f"/articles/{article['article_id']}/revisions",body,etag=article['etag']);assert r.status_code==201,r.text
    stale=req(client,tokens,'POST',f"/articles/{article['article_id']}/revisions",body,etag=article['etag']);assert stale.status_code==412
    with engine.begin() as c:assert len(rows(c,'article_revision',article_id=int(article['article_id'])))==1

def test_database_constraints(system):
    client,engine,tokens=system
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as c:add(c,'claim_evidence',claim_id=999,evidence_id=None,result_id=None,relation_type='SUPPORT',note='invalid')
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as c:add(c,'turnout_observation',dataset_id=999,election_id=999,region_id=999,eligible_voters=10,voters=20,official_turnout=None,official_decimals=None,calculated_turnout=200)

def test_frozen_and_immutable_database_guards(demo):
    client,engine,tokens=demo
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as c:update(c,'turnout_observation',1,voters=1)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as c:update(c,'article_revision',1,title='alter approved article')
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as c:update(c,'approval',1,decision='REVOKED')

def test_rule_checker_detects_number_and_unit_mismatches(demo):
    client,engine,tokens=demo
    article=req(client,tokens,'POST','/articles',{'slug':'incorrect-number'}).json()['data']
    text='2016년 투표율은 99.00%p다.'
    body={'base_revision_id':None,'title':'검사 오류 예제','body':{'blocks':[{'block_id':'x','type':'PARAGRAPH','text':text}]},'claims':[{'claim_no':1,'text':text,'body_locator':f'x:0:{len(text)}','is_core':True,'is_numeric':True,'evidence':[{'result_id':1,'note':'incorrect number and unit test'}]}],'change_note':'검사 예제'}
    r=req(client,tokens,'POST',f"/articles/{article['article_id']}/revisions",body,etag=article['etag']);assert r.status_code==201,r.text
    rid=r.json()['data']['revision_id'];tag=get(client,tokens,f'/revisions/{rid}').headers['etag']
    j=req(client,tokens,'POST',f'/revisions/{rid}/checks',{'rule_version':'v1'},etag=tag);assert j.status_code==202,j.text;tick(engine)
    checks=get(client,tokens,f'/revisions/{rid}/checks?rule_version=v1').json()['data']
    assert {x['category'] for x in checks if x['status']=='FAIL'}=={'NUMBER','UNIT'}

def test_cursor_binding_and_job_ownership(demo):
    client,engine,tokens=demo
    r=get(client,tokens,'/analysis-runs/1/results?limit=2');assert r.json()['page']['has_more']
    cursor=r.json()['page']['next_cursor']
    nextpage=get(client,tokens,'/analysis-runs/1/results?limit=2&cursor='+cursor)
    assert r.json()['data'][0]['result_id']!=nextpage.json()['data'][0]['result_id']
    assert req(client,tokens,'GET','/analysis-runs/1/results?limit=3&cursor='+cursor).status_code==400
    with engine.begin() as c:job=rows(c,'api_job')[0]['job_id']
    assert req(client,tokens,'GET','/jobs/'+job,who='reviewer').status_code==403

def test_public_api_does_not_expose_internal_paths(demo):
    client,engine,tokens=demo
    for path in ['/methodology','/files','/articles','/summary','/fact-checks']:
        text=client.get('/api/v1/public'+path+'?publication_id=1').text
        assert 'storage_uri' not in text and 'token_hash' not in text

def test_66_designed_operations_have_routes(system):
    client,engine,tokens=system
    spec=client.get('/openapi.json').json()
    operations=[(path,method) for path,methods in spec['paths'].items() for method in methods if method in ('get','post','put','patch','delete') and path.startswith('/api/v1/')]
    assert len(operations)==66

def test_postgres_smoke_if_configured():
    url=os.getenv('TEST_DATABASE_URL')
    if not url:pytest.skip('PostgreSQL integration runs in CI or with TEST_DATABASE_URL')
    engine=create_engine(url);initialize(engine)
    with engine.connect() as c:
        tr=c.begin();sid=add(c,'source_document',title='transaction test',publisher='CI',published_on=None,source_url=None,usage_terms='test')
        assert one(c,'source_document',sid)['title']=='transaction test';tr.rollback()
