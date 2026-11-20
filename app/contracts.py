from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Role = Literal['ANALYST','SEARCHER','REVIEWER','EDITOR','LEAD']
Verdict = Literal['SUPPORTED','INCORRECT','UNVERIFIABLE','MIXED']
class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class SourceInput(Input):
    title:str=Field(min_length=1,max_length=200)
    publisher:str=Field(min_length=1,max_length=200)
    published_on:str|None=None
    source_url:str|None=None
    usage_terms:str=Field(min_length=1,max_length=5000)
class DatasetInput(Input):
    base_dataset_id:int|None=None
    change_note:str=Field(min_length=1,max_length=5000)
class ImportInput(Input):
    file_ids:list[int]=Field(min_length=1,max_length=20)
    importer_version:str='csv-v1'
    options:dict=Field(default_factory=dict)
class CheckInput(Input):
    rule_version:str=Field(min_length=1,max_length=60)
class MappingInput(Input):
    region_id:int|None
    handling_note:str=Field(min_length=1,max_length=5000)
class Member(Input):
    region_id:int
    is_included:bool
    reason:str=Field(min_length=1,max_length=5000)
class MembersInput(Input):
    members:list[Member]=Field(min_length=1,max_length=1000)
class AnalysisInput(Input):
    dataset_id:int
    code_ref:Literal['builtin-v1']='builtin-v1'
    parameters:dict=Field(default_factory=dict)
class ChartInput(Input):
    chart_key:str=Field(min_length=1,max_length=60)
    title:str=Field(min_length=1,max_length=300)
    chart_spec:dict=Field(default_factory=dict)
    result_ids:list[int]=Field(min_length=1,max_length=1000)
class SearchInput(Input):
    query:str=Field(min_length=1,max_length=1000)
    filters:dict=Field(default_factory=dict)
    limit:int=Field(default=10,ge=1,le=50)
class ArticleInput(Input):
    slug:str=Field(pattern=r'^[a-z0-9][a-z0-9-]{0,159}$')
class EvidenceInput(Input):
    evidence_id:int|None=None
    result_id:int|None=None
    relation_type:Literal['SUPPORT','CONTRADICT','CONTEXT']='SUPPORT'
    note:str=Field(min_length=1,max_length=5000)
class ClaimInput(Input):
    claim_no:int=Field(ge=1)
    text:str=Field(min_length=1,max_length=5000)
    body_locator:str
    is_core:bool=True
    is_numeric:bool=True
    evidence:list[EvidenceInput]=Field(default_factory=list,max_length=100)
class BodyBlock(Input):
    block_id:str=Field(min_length=1,max_length=100)
    type:Literal['PARAGRAPH','HEADING','CHART']
    text:str|None=None
    chart_id:int|None=None
class ArticleBody(Input):
    blocks:list[BodyBlock]=Field(min_length=1,max_length=1000)
class RevisionInput(Input):
    base_revision_id:int|None
    title:str=Field(min_length=1,max_length=300)
    body:ArticleBody
    claims:list[ClaimInput]=Field(max_length=500)
    change_note:str=Field(min_length=1,max_length=5000)
class FactInput(Input):
    claim_id:int
    method:str=Field(min_length=1,max_length=5000)
    finding:str=Field(min_length=1,max_length=5000)
    verdict:Verdict
class DecisionInput(Input):
    decision:Literal['APPROVED','CHANGES','HOLD','REVOKED']
    review_note:str=Field(min_length=1,max_length=5000)
class Item(Input):
    revision_id:int|None=None
    chart_id:int|None=None
class PublicationInput(Input):
    item_refs:list[Item]=Field(min_length=1,max_length=1000)
    correction_note:str|None=None
class ItemsInput(Input):
    items:list[Item]=Field(min_length=1,max_length=1000)
class FreezeInput(Input):
    report_file_id:int
    journal_file_id:int
class NoteInput(Input):
    change_note:str=Field(min_length=1,max_length=5000)
class WithdrawInput(Input):
    correction_note:str=Field(min_length=1,max_length=5000)
class FileCheckInput(Input):
    file_ids:list[int]=Field(min_length=1,max_length=1000)
class DraftInput(Input):
    run_id:int
    evidence_ids:list[int]=Field(max_length=100)
    instructions:str=Field(min_length=1,max_length=5000)
class ExtractInput(Input):
    extractor_version:Literal['text-v1']='text-v1'
class RolesInput(Input):
    roles:list[Role]=Field(min_length=1)
    reason:str=Field(min_length=1,max_length=5000)
