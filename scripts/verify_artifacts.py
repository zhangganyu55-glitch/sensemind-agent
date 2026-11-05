"""Verify trusted local model copies against committed run manifests."""
import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
for name in sys.argv[1:] or ['runs/20261030-pamap2-activity.json','runs/20261031-ppg-dalia-rf-local.json']:
    for artifact in json.loads((root/name).read_text())['artifacts']:
        path=(root/artifact['path']).resolve()
        if not path.is_relative_to(root/'artifacts'):
            raise ValueError('Artifact outside model directory')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=artifact['sha256']:
            raise SystemExit('SHA-256 mismatch: '+artifact['path'])
        print('verified',artifact['path'])
