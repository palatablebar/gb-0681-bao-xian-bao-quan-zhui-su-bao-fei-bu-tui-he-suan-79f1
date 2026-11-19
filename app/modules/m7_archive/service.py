from app.modules.m7_archive.export_out import checksum

def execute(state):
    correction_balance=state.get('correction_balance')
    correction_ok=True if correction_balance is None else correction_balance.get('balanced',False)
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+len(state.get('corrections',[])),
        'ready':state.get('reconcile',{}).get('balanced',False) and correction_ok}
    return state
