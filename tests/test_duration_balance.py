import json

from autoclip.budget import spoken_characters
from autoclip.models import Plan
from autoclip.narrative import commentary_balance_issues, require_commentary, NARRATIVE_CRITERIA
from autoclip.util import read_json, write_json
from test_commentary_balance import fixture


def short_fixture():
    plan,lines,cfg,_=fixture()
    plan.clips[0].beats=plan.clips[0].beats[:34]
    for beat in plan.clips[0].beats:
        if beat.mode=='narration':
            beat.text='据'*101
            beat.end=beat.start+30
    lines=[s for s in lines if s.id<68]
    return plan,lines,cfg


def measured(plan,cfg):
    from autoclip.repair import timing_report
    return timing_report(plan.clips[0],{i:spoken_characters(b.text)/4.5-.35
        for i,b in enumerate(plan.clips[0].beats) if b.mode=='narration'},cfg)


def test_duration_deficit_uses_meaningful_commentary_even_when_quantity_already_passes():
    plan,lines,cfg=short_fixture();timing=measured(plan,cfg)
    assert require_commentary(plan.clips[0],cfg,timing)['narration_chars']==1818
    assert [i['code'] for i in timing['issues']]==['duration_short']
    issues=commentary_balance_issues(plan.clips[0],cfg,timing,lines)
    assert 1<=len(issues)<=3
    assert all(i['stage']=='commentary_balance' and '总时长尚差' in i['message'] for i in issues)
    assert sum(i['minimum_chars']-101 for i in issues)>=160


def test_duration_only_shortfall_selects_core_units_instead_of_rewriting_every_narration(tmp_path,monkeypatch):
    import autoclip.series as module
    plan,lines,cfg=short_fixture();events=[];targets=[]
    def measure(video,path,transcript,cfg,root):
        events.append('measure');current=Plan.model_validate(read_json(path))
        write_json(root/'plan-timed.json',current.model_dump());return measured(current,cfg)
    def audit(*args):events.append('audit');return {'passed':True,'issues':[]}
    class Model:
        def json(self,system,prompt,images=None):
            if '只选择有真实讲述价值' in system:
                events.append('choose');return {'choices':[{'beat':i,'reason':'补充当前主线的必要约束','evidence_ids':[i*2,i*2+1]} for i in (0,1)]}
            if '局部修稿编辑' in system:
                target=json.JSONDecoder().raw_decode(prompt.split('\ntarget=',1)[1])[0]
                issues=json.JSONDecoder().raw_decode(prompt.split('\n问题',1)[1])[0]
                minimum=max(i['minimum_chars'] for i in issues)
                targets.append(target['id']);events.append('write')
                return {'changes':[{'id':target['id'],'text':'据'*minimum}]}
            events.append('whole')
            return {'checks':[{'criterion':c,'passed':True,'reason':'通过'} for c in sorted(NARRATIVE_CRITERIA)],'issues':[]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',audit)
    monkeypatch.setattr(module,'duration',lambda _:1360)
    def no_visual_padding(*a,**k):raise AssertionError('纯时长不足应补有据讲述，不加静帧或挨段盲改')
    monkeypatch.setattr('autoclip.recut.cut_candidates',no_visual_padding)
    final,report=module.complete_long_plan(tmp_path/'video.mp4',plan,lines,{'lines':[]},tmp_path,cfg,Model())
    assert report['passed'] and measured(final,cfg)['measured_total_seconds']>=1080
    assert targets==['beat-0','beat-1'] and len(targets)<18
    assert events==['measure','choose','write','write','measure','whole','audit']
    budget=read_json(tmp_path/'repair-budget.json')['used']
    assert budget['commentary']==1 and budget['timing']==0
    assert final.clips[0].beats[2:]==plan.clips[0].beats[2:]
