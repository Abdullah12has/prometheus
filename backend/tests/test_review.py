def test_changed_contact_loses_verification(client):
    company = client.post('/api/companies', json={'name': 'Contact verification'}).json()['company']['id']
    contact = client.post(f'/api/companies/{company}/contacts', json={'name': 'Owner', 'email': 'owner@example.com'}).json()
    verified = client.post(f"/api/contacts/{contact['id']}/verify", json={'basis': 'Confirmed directly with company owner'})
    assert verified.status_code == 200
    assert verified.json()['verification'] == 'verified'
    changed = client.patch(f"/api/contacts/{contact['id']}", json={'email': 'different@example.com'})
    assert changed.status_code == 200
    assert changed.json()['verification'] == 'unverified'


def test_financial_review_retains_original_source_and_value(client):
    company = client.post('/api/companies', json={'name': 'Financial review'}).json()['company']['id']
    source = client.post('/api/sources', json={'kind': 'document', 'title': 'Owner supplied annual accounts'}).json()['id']
    row = client.post(f'/api/companies/{company}/financials', json={'source_id': source, 'metric': 'revenue', 'amount': '123456.78', 'currency': 'EUR', 'period_start': '2025-01-01', 'period_end': '2025-12-31', 'scope': 'entity', 'status': 'reported'}).json()
    assert row['review_status'] == 'proposed'
    reviewed = client.post(f"/api/financials/{row['id']}/review", json={'status': 'accepted', 'reason': 'Checked against signed accounts'}).json()
    assert reviewed['review_status'] == 'accepted'
    assert reviewed['amount'] == row['amount']
    assert reviewed['source']['id'] == source
