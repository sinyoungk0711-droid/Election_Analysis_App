"""Portable SQLAlchemy Core schema derived from the reviewed data dictionary."""
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv('APP_DATA_DIR', str(ROOT / '.runtime'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
metadata = sa.MetaData()
catalog = json.loads(Path(__file__).with_name('schema_catalog.json').read_text(encoding='utf-8'))

def now():
    return datetime.now(timezone.utc)

def dtype(value):
    if value == 'bigint':
        return sa.BigInteger().with_variant(sa.Integer, 'sqlite')
    if value in ('integer', 'smallint'):
        return sa.Integer()
    if value == 'boolean':
        return sa.Boolean()
    if value == 'jsonb':
        return sa.JSON()
    if value == 'date':
        return sa.Date()
    if value == 'timestamptz':
        return sa.DateTime(timezone=True)
    if value.startswith('numeric'):
        a, b = map(int, re.findall(r'\d+', value))
        return sa.Numeric(a, b)
    if value.startswith(('varchar', 'char')):
        return sa.String(int(re.search(r'\d+', value)[0]))
    return sa.Text()

T = {}
for name, title, fields, rule in catalog:
    columns = []
    pk_count = sum('PK' in f[2] for f in fields)
    for column, typ, flags, description in fields:
        args = []
        target = re.search(r'\b([a-z_]+)\.([a-z_]+)\b', description)
        if 'FK' in flags and target:
            args.append(sa.ForeignKey('.'.join(target.groups()), ondelete='RESTRICT'))
        columns.append(sa.Column(column, dtype(typ), *args,
            primary_key='PK' in flags, nullable='선택' in flags,
            unique='UQ' in flags and 'PK' not in flags,
            autoincrement=True if 'PK' in flags and pk_count == 1 and typ == 'bigint' else 'auto'))
    if name in ('dataset_version', 'article', 'fact_check', 'publication', 'app_user'):
        columns.append(sa.Column('revision_token', sa.Integer, nullable=False, default=1))
    if name == 'approval':
        columns.append(sa.Column('snapshot_token', sa.Integer, nullable=False, default=1))
    if name == 'publication':
        columns.extend([sa.Column('report_file_id', sa.BigInteger), sa.Column('journal_file_id', sa.BigInteger)])
    T[name] = sa.Table(name, metadata, *columns, comment=title)

uniques = {
 'region_mapping': [('dataset_id','election_id','file_id','source_row_key')],
 'turnout_observation': [('dataset_id','election_id','region_id')],
 'metric_definition': [('metric_code','definition_version')],
 'analysis_result': [('run_id','metric_id','dimension_key')],
 'chart': [('chart_key','version_no')], 'chart_result': [('chart_id','display_order')],
 'article_revision': [('article_id','version_no')], 'claim': [('revision_id','claim_no')],
 'fact_check_revision': [('fc_id','version_no')],
 'publication_item': [('publication_id','display_order')],
 'handover_file': [('publication_id','storage_uri')],
}
for table, sets in uniques.items():
    for cols in sets:
        T[table].append_constraint(sa.UniqueConstraint(*cols))
T['observation_lineage'].append_constraint(sa.UniqueConstraint('mapping_id'))
T['turnout_observation'].append_constraint(sa.CheckConstraint('eligible_voters > 0 AND voters >= 0 AND voters <= eligible_voters'))
T['turnout_observation'].append_constraint(sa.CheckConstraint('official_turnout IS NULL OR (official_turnout >= 0 AND official_turnout <= 100)'))
for table, cols in [('claim_evidence',['evidence_id','result_id']),
                    ('publication_item',['revision_id','chart_id']),
                    ('approval',['dataset_id','run_id','fc_revision_id','revision_id','publication_id'])]:
    T[table].append_constraint(sa.CheckConstraint(' + '.join(f'CASE WHEN {c} IS NULL THEN 0 ELSE 1 END' for c in cols) + ' = 1'))

T['access_token'] = sa.Table('access_token', metadata,
 sa.Column('token_hash',sa.String(64),primary_key=True),
 sa.Column('user_id',sa.BigInteger,sa.ForeignKey('app_user.user_id'),nullable=False),
 sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False))
T['api_job'] = sa.Table('api_job',metadata,
 sa.Column('job_id',sa.String(36),primary_key=True), sa.Column('actor_id',sa.BigInteger,sa.ForeignKey('app_user.user_id'),nullable=False),
 sa.Column('type',sa.String(32),nullable=False),sa.Column('status',sa.String(16),nullable=False),
 sa.Column('payload',sa.JSON,nullable=False),sa.Column('result',sa.JSON),sa.Column('error',sa.JSON),
 sa.Column('progress',sa.Integer,nullable=False,default=0),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
 sa.Column('finished_at',sa.DateTime(timezone=True)),sa.Column('lease_until',sa.DateTime(timezone=True)),
 sa.Column('lease_owner',sa.String(36)),sa.Column('attempts',sa.Integer,nullable=False,default=0))
T['api_idempotency'] = sa.Table('api_idempotency',metadata,
 sa.Column('actor_id',sa.BigInteger,primary_key=True),sa.Column('key',sa.String(80),primary_key=True),
 sa.Column('method_path',sa.Text,primary_key=True),sa.Column('request_hash',sa.String(64),nullable=False),
 sa.Column('response',sa.JSON,nullable=False),sa.Column('http_status',sa.Integer,nullable=False),
 sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
T['schema_version'] = sa.Table('schema_version', metadata, sa.Column('version',sa.Integer,primary_key=True))

def create_engine(url=None):
    url = url or os.getenv('DATABASE_URL', 'sqlite:///' + str(DATA / 'election.db').replace('\\','/'))
    engine = sa.create_engine(url, connect_args={'check_same_thread':False,'timeout':30} if url.startswith('sqlite') else {}, pool_pre_ping=True)
    if engine.dialect.name == 'sqlite':
        @sa.event.listens_for(engine,'connect')
        def configure(conn, record):
            conn.isolation_level = None
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA journal_mode=WAL')
        @sa.event.listens_for(engine,'begin')
        def begin_immediate(conn):
            conn.exec_driver_sql('BEGIN IMMEDIATE')
    return engine

def initialize(engine):
    with engine.begin() as c:
        if engine.dialect.name=='postgresql': c.execute(sa.text('SELECT pg_advisory_xact_lock(7102016)'))
        metadata.create_all(c)
        if not c.execute(sa.select(T['schema_version'])).first():
            c.execute(T['schema_version'].insert().values(version=1))
        immutable=('source_file','evidence_chunk','metric_definition','analysis_result','chart','chart_result','article_revision','claim','claim_evidence','fact_check_revision','approval','audit_event')
        if engine.dialect.name=='sqlite':
            for name in immutable:
                for action in ('UPDATE','DELETE'):
                    c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS protect_{name}_{action.lower()} BEFORE {action} ON {name} BEGIN SELECT RAISE(ABORT, 'immutable version'); END")
            for name in ('region_mapping','turnout_observation','comparison_member'):
                for action in ('INSERT','UPDATE','DELETE'):
                    ref='OLD' if action=='DELETE' else 'NEW'
                    condition=f"(SELECT status FROM dataset_version WHERE dataset_id={ref}.dataset_id)='FROZEN'"
                    if action=='UPDATE': condition+=" OR (SELECT status FROM dataset_version WHERE dataset_id=OLD.dataset_id)='FROZEN'"
                    c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS frozen_{name}_{action.lower()} BEFORE {action} ON {name} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'frozen dataset'); END")
            for name in ('publication_item','handover_file'):
                for action in ('INSERT','UPDATE','DELETE'):
                    ref='OLD' if action=='DELETE' else 'NEW'
                    c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS frozen_{name}_{action.lower()} BEFORE {action} ON {name} WHEN (SELECT status FROM publication WHERE publication_id={ref}.publication_id)!='DRAFT' BEGIN SELECT RAISE(ABORT, 'frozen publication'); END")
        else:
            c.exec_driver_sql("""CREATE OR REPLACE FUNCTION prevent_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable version' USING ERRCODE='23514'; END $$""")
            for name in immutable:
                c.exec_driver_sql(f"DO $$ BEGIN CREATE TRIGGER protect_{name} BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION prevent_version_mutation(); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            c.exec_driver_sql("""CREATE OR REPLACE FUNCTION protect_frozen_parent() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE parent_id bigint; state text;
            BEGIN
              IF TG_OP = 'DELETE' THEN
                IF TG_ARGV[0] = 'dataset' THEN parent_id := OLD.dataset_id; ELSE parent_id := OLD.publication_id; END IF;
              ELSE
                IF TG_ARGV[0] = 'dataset' THEN parent_id := NEW.dataset_id; ELSE parent_id := NEW.publication_id; END IF;
              END IF;
              IF TG_ARGV[0] = 'dataset' THEN SELECT status INTO state FROM dataset_version WHERE dataset_id=parent_id;
              ELSE SELECT status INTO state FROM publication WHERE publication_id=parent_id; END IF;
              IF (TG_ARGV[0]='dataset' AND state='FROZEN') OR (TG_ARGV[0]='publication' AND state<>'DRAFT') THEN RAISE EXCEPTION 'frozen parent' USING ERRCODE='23514'; END IF;
              IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END $$""")
            for name in ('region_mapping','turnout_observation','comparison_member','publication_item','handover_file'):
                kind='publication' if name in ('publication_item','handover_file') else 'dataset'
                c.exec_driver_sql(f"DO $$ BEGIN CREATE TRIGGER frozen_{name} BEFORE INSERT OR UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION protect_frozen_parent('{kind}'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")

def rows(c, table, **filters):
    t=T[table]; query=sa.select(t)
    for key,value in filters.items(): query=query.where(t.c[key]==value)
    return [dict(r) for r in c.execute(query).mappings()]

def one(c,table,pk):
    t=T[table]; key=list(t.primary_key.columns)[0]
    row=c.execute(sa.select(t).where(key==pk)).mappings().first()
    if not row: raise KeyError(f'{table}:{pk}')
    return dict(row)

def add(c,table,**values):
    t=T[table]
    for col in t.columns:
        if col.name not in values and not col.nullable and not col.primary_key:
            if col.default is not None: continue
            if isinstance(col.type,sa.DateTime): values[col.name]=now()
    result=c.execute(t.insert().values(**values))
    return result.inserted_primary_key[0]

def update(c,table,pk,**values):
    t=T[table]; key=list(t.primary_key.columns)[0]
    c.execute(t.update().where(key==pk).values(**values))

def serialize(value, key=''):
    if isinstance(value,dict): return {k:serialize(v,k) for k,v in value.items() if k not in ('storage_uri','token_hash')}
    if isinstance(value,(list,tuple)): return [serialize(v,key) for v in value]
    if isinstance(value,Decimal): return format(value,'f')
    if isinstance(value,datetime): return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat().replace('+00:00','Z')
    if hasattr(value,'isoformat'): return value.isoformat()
    if isinstance(value,int) and not isinstance(value,bool) and (key.endswith(('_id','_ids')) or key in ('ids','voters','eligible_voters')): return str(value)
    return value
