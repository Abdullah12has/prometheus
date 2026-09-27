"""Public investor discovery; candidates still require official-site qualification."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

import httpx

from . import acquisition
from .identity import normalize_website

COUNTRIES = ('DK', 'FI', 'IS', 'NO', 'SE', 'CH', 'DE')
SOURCES = {
    'svca': {'id': 'svca', 'country': 'SE', 'label': 'SVCA growth and buyout members', 'url': 'https://www.svca.se/ordinarie-medlemmar/', 'coverage': 'Public growth-capital and buyout members. Membership indicates regional activity, not headquarters or current demand.'},
    'nvca': {'id': 'nvca', 'country': 'NO', 'label': 'NVCA direct investors', 'url': 'https://www.nvca.no/medlem-medlemmer/vare-medlemmer', 'coverage': 'Family offices, growth/buyout and other investing firms. Excludes advisers, fund allocators and VC-only categories.'},
    'fvca': {'id': 'fvca', 'country': 'FI', 'label': 'Finnish private-equity association', 'url': 'https://paaomasijoittajat.fi/en/members/member-directory/?type=general_partner', 'coverage': 'General partners and other private-equity investors. Candidates are qualified on their own websites; membership is not a confirmed mandate.'},
    'aktive_ejere': {'id': 'aktive_ejere', 'country': 'DK', 'label': 'Aktive Ejere investors', 'url': 'https://aktiveejere.dk/alle-medlemmer/', 'coverage': 'Public PE and family-investment members. The association reports technical issues and incomplete member display.'},
    'bvk': {'id': 'bvk', 'country': 'DE', 'label': 'BVK investment members', 'url': 'https://www.bvkap.de/der-bvk/mitglieder?gruppe=2', 'coverage': 'Ordinary investment members, with pagination. Includes international firms active in Germany and candidates requiring qualification.'},
}
for country in COUNTRIES:
    SOURCES[f'official_{country.lower()}'] = {
        'id': f'official_{country.lower()}', 'country': country, 'label': f'{country} official investor websites',
        'url': '', 'coverage': 'Documented official firm sites fill directory gaps. Family-office coverage is partial; unpublished firms cannot be enumerated. Swiss SECA member data is not used because of its stated usage restriction.' if country == 'CH' else 'Documented official firm sites fill directory gaps. Public family-office coverage is partial.',
    }

BLOCKED_DOMAINS = {'linkedin.com', 'facebook.com', 'instagram.com', 'youtube.com', 'twitter.com', 'x.com', 'google.com', 'google.dk', 'grouponline.dk', 'getynet.com', 'dcode.no', 'amzn.eu', 'easy-feedback.de', 'german-impact-investing.com', 'vimeo.com', 'w3.org', 'wordpress.org'}


class DirectoryPage(acquisition._PageExtractor):
    """Collect anchor labels and the directory's own category headings."""
    def __init__(self, url: str):
        super().__init__(url)
        self.anchors: list[dict] = []
        self.headings: list[str] = []
        self.category = ''
        self._heading_tag = ''
        self._heading_text = ''
        self._anchor: dict | None = None
        self.members: list[tuple[str, str]] = []
        self._div_depth = 0
        self._card_depth = -1
        self._tags = ''

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        fields = dict(attrs)
        if tag == 'div':
            self._div_depth += 1
            if 'data-tag' in fields:
                self._card_depth, self._tags = self._div_depth, fields['data-tag'] or ''
        if tag in ('h1', 'h2', 'h3'):
            self._heading_tag, self._heading_text = tag, ''
        if tag == 'a' and fields.get('href'):
            self._anchor = {'url': urljoin(self.base_url, fields['href']), 'text': '', 'category': self.category, 'attrs': fields, 'tags': self._tags}
        if tag == 'button' and 'more' in (fields.get('class') or '').split() and (fields.get('id') or '').isdigit():
            self.members.append((self.headings[-1] if self.headings else '', fields['id']))
        if tag == 'img' and self._anchor and fields.get('alt'):
            self._anchor['text'] += ' ' + fields['alt']

    def handle_data(self, data):
        super().handle_data(data)
        if self._skip_depth:
            return
        if self._heading_tag:
            self._heading_text += data
        if self._anchor:
            self._anchor['text'] += data

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'div':
            if self._div_depth == self._card_depth:
                self._tags, self._card_depth = '', -1
            self._div_depth -= 1
        if tag == self._heading_tag:
            value = ' '.join(self._heading_text.split())
            if value:
                self.headings.append(value)
                if tag == 'h3':
                    self.category = value
            self._heading_tag = ''
        if tag == 'a' and self._anchor:
            self._anchor['text'] = ' '.join(self._anchor['text'].split())
            self.anchors.append(self._anchor)
            self._anchor = None


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _firm_link(url: str, directory_domain: str) -> bool:
    try:
        _, domain = normalize_website(url)
    except ValueError:
        return False
    return (domain != directory_domain and not any(domain == blocked or domain.endswith('.' + blocked) for blocked in BLOCKED_DOMAINS)
            and not urlsplit(url).path.lower().endswith(('.pdf', '.jpg', '.png', '.svg', '.zip', '.css', '.js')))


def _candidate(name, website, country, kind, source_url):
    canonical, _ = normalize_website(website)
    return {'name': name.strip()[:300], 'website': canonical, 'country': country, 'kind': kind, 'source_url': source_url}


def discover(source_id: str, beat) -> dict:
    info = SOURCES[source_id]
    candidates, errors, pages = [], [], 0
    robots_cache = {}

    def read(url: str, form: dict | None = None):
        nonlocal pages
        beat()
        origin = f'{urlsplit(url).scheme}://{urlsplit(url).netloc}'
        if origin not in robots_cache:
            robots_cache[origin] = acquisition._load_robots(origin, 12)
        robots, problem = robots_cache[origin]
        if robots is None or not robots.can_fetch(acquisition.USER_AGENT, url):
            errors.append(f'{url}: {problem or "robots_disallowed"}')
            return None
        try:
            if form is not None:
                # This is the public popup handler called by the directory's own JavaScript.
                # The endpoint is constant, numeric member IDs come only from its public cards.
                if url != 'https://paaomasijoittajat.fi/wp/wp-admin/admin-ajax.php' or form.get('action') != 'more' or not str(form.get('id', '')).isdigit():
                    raise ValueError('Invalid public member request')
                with httpx.stream('POST', url, data=form, timeout=15, follow_redirects=False,
                                  headers={'User-Agent': acquisition.USER_AGENT}) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 3 * 1024 * 1024:
                            raise ValueError('Member response too large')
                doc = DirectoryPage(url)
                doc.feed(json.loads(raw)['result'])
                pages += 1
                time.sleep(.15)
                return doc
            result = acquisition.fetch_public_url(url, timeout=15, max_bytes=3 * 1024 * 1024, allow_cross_host_redirects=False)
            if not robots.can_fetch(acquisition.USER_AGENT, result.final_url):
                errors.append(f'{url}: robots_disallowed_redirect')
                return None
            pages += 1
            if result.status != 200 or not result.text:
                errors.append(f'{url}: HTTP {result.status} or empty page')
                return None
            doc = DirectoryPage(result.final_url)
            doc.feed(result.text)
            time.sleep(.15)  # one polite serial request stream per directory
            return doc
        except (acquisition.FetchError, ValueError, KeyError, httpx.HTTPError) as exc:
            errors.append(f'{url}: {str(exc)[:240]}')
            return None

    if source_id.startswith('official_'):
        seed_file = Path(__file__).with_name('buyer_seeds.json')
        seeds = json.loads(seed_file.read_text()) if seed_file.exists() else []
        for seed in seeds:
            if seed['country'] != info['country']:
                continue
            beat()
            # Discovery provenance points to an inspected primary source. The queued research
            # worker fetches it again and may reject the candidate if the evidence has changed.
            candidates.append(_candidate(seed['name'], seed['website'], seed['country'], seed['kind'], seed['source_url']))
    elif source_id in ('svca', 'nvca'):
        doc = read(info['url'])
        allowed = {'Growth Capital': 'private_equity', 'Buyout': 'private_equity'} if source_id == 'svca' else {
            'Familiekontorer': 'family_office', 'Vekst- og oppkjøpsfond': 'private_equity', 'Andre investerende miljøer': 'holding_company'}
        if doc:
            for anchor in doc.anchors:
                kind = allowed.get(anchor['category'])
                if kind and anchor['text'] and _firm_link(anchor['url'], _domain(info['url'])):
                    candidates.append(_candidate(anchor['text'], anchor['url'], info['country'], kind, info['url']))
    elif source_id == 'bvk':
        pending, seen, profiles = [info['url']], set(), set()
        while pending and len(seen) < 30:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            doc = read(url)
            if not doc:
                continue
            for anchor in doc.anchors:
                href = anchor['url']
                if _domain(href) != 'bvkap.de':
                    continue
                if '/mitglieder-details/user/' in urlsplit(href).path:
                    profiles.add(href)
                elif urlsplit(href).path == '/der-bvk/mitglieder' and parse_qs(urlsplit(href).query).get('gruppe') == ['2'] and 'page_e54=' in href and href not in seen:
                    pending.append(href)
        for url in sorted(profiles):
            doc = read(url)
            if not doc:
                continue
            links = [a for a in doc.anchors if _firm_link(a['url'], 'bvkap.de')]
            if links:
                name = next((h for h in doc.headings if h not in ('Kontakt', 'Mitglieder', 'Bitte Sprechen Sie uns an:')), '')
                if not name:
                    name = (doc.title or '').split('|')[0].strip()
                if name:
                    candidates.append(_candidate(name, links[0]['url'], 'DE', 'unknown', url))
    elif source_id in ('fvca', 'aktive_ejere'):
        # Dedicated adapters use the public pages' actual controls; no private APIs or guessed paths.
        candidates.extend(_local_directory(source_id, info, read, errors))
    unique = {}
    for item in candidates:
        unique.setdefault(_domain(item['website']), item)
    return {'candidates': list(unique.values()), 'errors': errors[:50], 'pages': pages}


def _local_directory(source_id, info, read, errors):
    found = []
    if source_id == 'fvca':
        seen = set()
        for category in ('general_partner', 'other_private_equity_investors'):
            directory = f'https://paaomasijoittajat.fi/en/members/member-directory/?type={category}'
            page = read(directory)
            if not page:
                continue
            for name, member_id in page.members:
                if member_id in seen:
                    continue
                seen.add(member_id)
                detail = read('https://paaomasijoittajat.fi/wp/wp-admin/admin-ajax.php', {'action': 'more', 'id': member_id, 'data': ''})
                links = [a for a in detail.anchors if _firm_link(a['url'], 'paaomasijoittajat.fi')] if detail else []
                if links:
                    found.append(_candidate(name, links[-1]['url'], 'FI', 'unknown', directory + f'#member-{member_id}'))
    else:
        page = read(info['url'])
        if not page:
            return found
        visited = set()
        for anchor in page.anchors:
            tags = set(anchor['tags'].split())
            if 'Rådgivende-medlemmer' in tags or not tags.intersection({'Kapitalfonde', 'Private-investeringsselskaber-herunder-Family-Offices'}):
                continue
            url = anchor['url']
            if not anchor['text'] or _domain(url) != 'aktiveejere.dk' or url in visited:
                continue
            visited.add(url)
            detail = read(url)
            links = [a for a in detail.anchors if _firm_link(a['url'], 'aktiveejere.dk')] if detail else []
            if links:
                kind = 'private_equity' if 'Kapitalfonde' in tags else 'family_office'
                found.append(_candidate(anchor['text'], links[0]['url'], 'DK', kind, url))
    return found
