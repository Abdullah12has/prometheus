import json
from pathlib import Path
from types import SimpleNamespace

from permetheus import buyer_sources as sources


def test_directory_parser_keeps_categories_cards_and_base_urls():
    p = sources.DirectoryPage('https://directory.test/members/page')
    p.feed('''<base href="https://directory.test/"><h3>Buyout</h3>
      <a href="profiles/firm">Firm Equity</a><h3>Limited Partners</h3><a href="profiles/pension">Pension</a>
      <div data-tag="Kapitalfonde"><div><a href="axcel">Axcel</a></div></div><a href="legal">Legal</a>
      <h2>CapMan</h2><button class="more" id="25784">Read more</button>''')
    assert p.anchors[0]['url'] == 'https://directory.test/profiles/firm'
    assert p.anchors[0]['category'] == 'Buyout'
    assert p.anchors[1]['category'] == 'Limited Partners'
    assert p.anchors[2]['tags'] == 'Kapitalfonde' and p.anchors[3]['tags'] == ''
    assert p.members == [('CapMan', '25784')]


def test_swedish_discovery_excludes_lp_vc_and_services_and_deduplicates(monkeypatch):
    monkeypatch.setattr(sources.acquisition, '_load_robots', lambda *a: (SimpleNamespace(can_fetch=lambda *a: True), None))
    html = '''<h3>Venture Capital</h3><a href="https://venture.test">Venture</a>
      <h3>Buyout</h3><a href="https://www.equity.test">Equity</a><a href="https://equity.test/about">Equity repeated</a>
      <a href="https://linkedin.com/eq">LinkedIn</a><h3>Limited Partners</h3><a href="https://pension.test">Pension</a>'''
    monkeypatch.setattr(sources.acquisition, 'fetch_public_url', lambda url, **kw: SimpleNamespace(final_url=url, status=200, text=html))
    monkeypatch.setattr(sources.time, 'sleep', lambda _: None)
    result = sources.discover('svca', lambda: None)
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['name'] == 'Equity' and result['candidates'][0]['kind'] == 'private_equity'
    assert result['errors'] == []
    monkeypatch.setattr(sources.acquisition, '_load_robots', lambda *a: (None, 'robots_unavailable'))
    assert sources.discover('svca', lambda: None)['candidates'] == []


def test_official_seed_coverage_and_provenance():
    records = json.loads(Path(sources.__file__).with_name('buyer_seeds.json').read_text())
    assert set(r['country'] for r in records) == set(sources.COUNTRIES)
    assert all(r['source_url'].startswith('https://') and r['source_note'] for r in records)
    result = sources.discover('official_ch', lambda: None)
    assert len(result['candidates']) >= 5
    assert all(r['country'] == 'CH' and r['source_url'] for r in result['candidates'])
