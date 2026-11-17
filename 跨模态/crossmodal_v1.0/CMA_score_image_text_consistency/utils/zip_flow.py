"""Native records forwarded to subsequent routes; scoring artifacts keep their own meaning."""
from copy import deepcopy


def build_flow_records(main, pending_model, not_applicable, input_order, route_enabled):
    positions = {key.strip(): index for index, key in enumerate(input_order)}
    forwarded = {}

    def put(record):
        key = record.get('pair_id')
        key = key.strip() if isinstance(key, str) else key
        if not isinstance(key, str) or key not in positions:
            raise ValueError('串联记录缺少当前输入 pair_id')
        if key in forwarded:
            raise ValueError('串联记录重复：%s' % key)
        forwarded[key] = deepcopy(record)

    for record in list(main) + list(pending_model):
        put(record)
    for ledger in not_applicable:
        source = ledger.get('record')
        if not isinstance(source, dict):
            raise ValueError('不适用台账缺少原生 record')
        record = deepcopy(source)
        processing = record.get('processing')
        if not isinstance(processing, dict):
            processing = {}
        processing['12'] = {'status': 1, 'process_status': 'not_applicable',
                            'route_enabled': bool(route_enabled) and ledger.get('reason') not in
                                             ('no_image', 'no_image_text', 'route_closed'),
                            'reason': ledger.get('reason'),
                            'tier': None, 'operator': '#12'}
        record['processing'] = processing
        record['image_text_score'] = {'status': 1, 'process_status': 'not_applicable',
                                      'clip_score': None, 'tier': None, 'reason': ledger.get('reason')}
        put(record)
    return sorted(forwarded.values(), key=lambda row: positions[row['pair_id'].strip()])
