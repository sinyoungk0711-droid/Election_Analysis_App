"""Regenerate contracts from the executable implementation; no credentials exported."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.main import app
from app.db import metadata
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

docs=ROOT/'docs';docs.mkdir(exist_ok=True)
spec=app.openapi()
(docs/'openapi.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 실행 API 목록','','`/api/v1` 아래 66개 설계 경로입니다. 실제 지원 제한은 IMPLEMENTATION.md를 확인하세요.','','|메서드|경로|operationId|','|---|---|---|']
for path,methods in spec['paths'].items():
    for method,op in methods.items():
        if path.startswith('/api/v1/') and method in ('get','post','patch','put','delete'):
            lines.append(f'|{method.upper()}|`{path}`|{op["operationId"]}|')
(docs/'endpoints.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
(docs/'schema.postgresql.sql').write_text('-- Generated initial schema. Protection triggers are installed by app.db.initialize.\n\n'+'\n\n'.join(str(CreateTable(t).compile(dialect=postgresql.dialect()))+';' for t in metadata.sorted_tables),encoding='utf-8')
print('Exported OpenAPI, endpoint inventory, PostgreSQL DDL.')
