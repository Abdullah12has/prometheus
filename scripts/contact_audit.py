"""Compare contact collection on a saved company sample using real public sources.

Runs the production gathering path without model extraction or outbound messages.
Reports contain public contact data; keep output under the ignored data/ directory.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
from pathlib import Path
import sys
import time

from permetheus.config import Settings
from permetheus.contacts import ContactIn
from permetheus import research


def load_snapshot(name, path):
    spec = importlib.util.spec_from_file_location(f'permetheus.audit_{name}', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--workers', type=int, default=3, choices=range(1, 5))
    args = parser.parse_args()
    module = research
    if args.baseline_dir:
        module = load_snapshot('research', args.baseline_dir / 'research_baseline.py')
        module.acquisition = load_snapshot('acquisition', args.baseline_dir / 'acquisition_baseline.py')
    settings = Settings()
    manifest = json.loads(args.manifest.read_text())
    class NoModel:
        configured = False
    def run(row):
        started = time.monotonic()
        findings = module.Findings()
        target = module.Target(row['name'], row['business_id'], row['domain'], row['website'], row['country'])
        error = None
        try:
            module._gather(target, findings, NoModel(), settings, lambda: None)
        except Exception as exc:
            error = f'{type(exc).__name__}: {str(exc)[:240]}'
        contacts = []
        for candidate in findings.contacts:
            try:
                valid = ContactIn(name=candidate.value, **{candidate.kind: candidate.value})
            except ValueError:
                continue
            contacts.append({'kind': candidate.kind, 'value': getattr(valid, candidate.kind),
                             'source_url': candidate.source_url, 'method': candidate.source})
        return {**row, 'elapsed_seconds': round(time.monotonic()-started, 1),
                'contacts': contacts, 'email_count': len({x['value'] for x in contacts if x['kind']=='email'}),
                'phone_count': len({x['value'] for x in contacts if x['kind']=='phone'}),
                'source_count': len(findings.fetched), 'checked': findings.checked,
                'missing': findings.missing, 'blocked': findings.blocked, 'errors': findings.errors,
                'fatal_error': error}
    results=[]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs={pool.submit(run,row): row for row in manifest}
        for job in as_completed(jobs):
            row=job.result();results.append(row)
            args.output.write_text(json.dumps(results,indent=2,ensure_ascii=False))
            print(f"{len(results)}/{len(manifest)} {row['country']} {row['name']}: {row['email_count']} emails, {row['phone_count']} phones, {row['source_count']} sources",flush=True)
    for country in ('FI','CH','DE'):
        for group in ('known_website','no_website'):
            rows=[r for r in results if r['country']==country and r['stratum']==group]
            print(country,group,'companies',len(rows),'with_email',sum(r['email_count']>0 for r in rows),'with_phone',sum(r['phone_count']>0 for r in rows),flush=True)


if __name__ == '__main__':
    main()
