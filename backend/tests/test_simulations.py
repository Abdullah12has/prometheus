import json
import uuid

from sqlalchemy import select, func
from permetheus.buyers import BuyerProfile, BuyerStatus
from permetheus.models import Company, Evidence, Source, SourceKind, ReviewStatus
from permetheus.deals import BuyerMandate, PreferenceProfile
from permetheus.llm import ModelUnavailable


class Model:
    configured = True
    def __init__(self, bad=False):
        self.bad = bad
    async def complete(self, messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        buyer = context['buyers'][0]
        return {'content': json.dumps({'matches': [{
            'buyer_id': buyer['id'], 'reason': 'Both sources describe industrial software.',
            'seller_refs': [context['seller']['evidence'][0]['ref']],
            'buyer_refs': ['invented'] if self.bad else [buyer['evidence'][0]['ref']],
        }]})}


def seed(client):
    with client.app.state.sessionmaker() as db:
        seller = Company(name='Seller Software', name_normalized='seller software', country='FI')
        buyer_co = Company(name='Buyer Group', name_normalized='buyer group', country='SE')
        db.add_all([seller,buyer_co]); db.flush()
        for co, field, value, url in [(seller,'industry','Industrial software','https://seller.example/about'),
                                      (buyer_co,'buyer_sector','Industrial software acquisitions','https://buyer.example/strategy')]:
            src=Source(kind=SourceKind.website,url=url,title=co.name)
            db.add(src); db.flush()
            db.add(Evidence(company_id=co.id,source_id=src.id,field=field,value=value,excerpt=value,
                            extraction_method='test',review_status=ReviewStatus.proposed))
        buyer=BuyerProfile(company_id=buyer_co.id,name='Buyer Group',name_normalized='buyer group',domain='buyer.example',
                           website='https://buyer.example',country='SE',status=BuyerStatus.profiled)
        db.add(buyer); db.commit()
        return str(seller.id),str(buyer.id)


def test_sourced_simulation_generates_branches_without_confirming_or_creating_mandates(client):
    seller,buyer=seed(client); client.app.state.llm=Model()
    r=client.post('/api/simulations',json={'company_id':seller})
    assert r.status_code==201,r.text
    run=r.json()
    assert run['buyers_evaluated']==1 and len(run['results'])==1
    result=run['results'][0]
    assert result['buyer_id']==buyer and result['status']=='research_needed'
    assert {s['url'] for s in result['sources']}=={'https://seller.example/about','https://buyer.example/strategy'}
    assert all(s['review_status']=='proposed' for s in result['sources'])
    assert len(result['scenarios'])==3
    assert all(s['hypothetical'] for s in result['scenarios'])
    assert all(s['checks'][0]['result']=='unknown' for s in result['scenarios'])
    assert result['decision_tree'][0]['no']=='Stop: no outreach'
    assert client.get('/api/simulations/'+run['id']).json()==run
    with client.app.state.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(BuyerMandate))==0
        assert db.scalar(select(func.count()).select_from(PreferenceProfile))==0
        row=db.scalar(select(Evidence).where(Evidence.company_id==uuid.UUID(seller)))
        row.value='Changed later'; db.commit()
    assert client.get('/api/simulations/'+run['id']).json()==run


def test_fabricated_citation_is_not_saved_as_a_recommendation(client):
    seller,_=seed(client); client.app.state.llm=Model(bad=True)
    r=client.post('/api/simulations',json={'company_id':seller})
    assert r.status_code==201
    assert r.json()['results']==[] and r.json()['rejected_recommendations']==1


def test_missing_evidence_and_excluded_buyer_are_actionable(client):
    seller,buyer=seed(client); client.app.state.llm=Model()
    with client.app.state.sessionmaker() as db:
        db.get(BuyerProfile,uuid.UUID(buyer)).status=BuyerStatus.excluded; db.commit()
    assert client.post('/api/simulations',json={'company_id':seller}).status_code==409
    empty=client.post('/api/companies',json={'name':'No evidence'}).json()['company']['id']
    assert client.post('/api/simulations',json={'company_id':empty}).status_code==409


def test_model_failure_does_not_persist_success(client):
    seller,_=seed(client)
    class Broken:
        async def complete(self,*args,**kwargs): raise ModelUnavailable('Unavailable')
    client.app.state.llm=Broken()
    assert client.post('/api/simulations',json={'company_id':seller}).status_code==503
    assert client.get('/api/simulations').json()==[]


def test_review_can_remove_weak_candidates(client):
    seller,_=seed(client)
    class Reviewed(Model):
        async def complete(self,messages,**kwargs):
            if 'proposed_matches' in json.loads(messages[-1]['content']):
                return {'content':'{"matches":[]}'}
            return await super().complete(messages,**kwargs)
    client.app.state.llm=Reviewed()
    response=client.post('/api/simulations',json={'company_id':seller})
    assert response.status_code==201 and response.json()['results']==[]


def test_scenarios_reuse_real_hard_constraints_without_mutating_owner_conditions():
    from permetheus.simulations import branches
    from test_deals import m_snap, PROFILE, FACTS
    profile={**PROFILE,'conditions':[{'kind':'team_retention','strength':'hard'}]}
    cases=branches(FACTS,profile,m_snap(structures=['full_sale'],max_rollover_pct='0',team_commitment=True))
    assert cases[0]['status']=='compatible'
    assert cases[1]['status']=='excluded'
    assert cases[2]['status']=='compatible'
    assert profile['conditions']==[{'kind':'team_retention','strength':'hard'}]


def test_self_and_rejected_evidence_cannot_supply_a_buyer(client):
    seller,buyer=seed(client); client.app.state.llm=Model()
    with client.app.state.sessionmaker() as db:
        profile=db.get(BuyerProfile,uuid.UUID(buyer))
        profile.company_id=uuid.UUID(seller); db.commit()
    assert client.post('/api/simulations',json={'company_id':seller}).status_code==409
