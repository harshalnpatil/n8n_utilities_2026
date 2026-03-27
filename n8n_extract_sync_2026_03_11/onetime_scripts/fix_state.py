#!/usr/bin/env python3
import json

state_path = r'n8n_extract_sync_2026_03_11\.n8n_sync\state.json'

with open(state_path, 'r', encoding='utf-8') as f:
    state = json.load(f)

fixed_records = {}
for key, record in state.get('records', {}).items():
    inst = record.get('instance')
    wf_id = record.get('workflowId', '')
    
    # Rebuild key properly
    new_key = inst + ':' + wf_id
    fixed_records[new_key] = record

state['records'] = fixed_records

with open(state_path, 'w', encoding='utf-8') as f:
    json.dump(state, f, indent=2)

cs = sum(1 for r in fixed_records.values() if r.get('instance') == 'cloud_secondary')
ct = sum(1 for r in fixed_records.values() if r.get('instance') == 'cloud_tertiary')
print('Fixed state.json: %d records (cs:%d ct:%d)' % (len(fixed_records), cs, ct))
