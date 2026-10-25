"""A truthful source fallback may need later growth; it never certifies a film."""
import pytest

from autoclip.automatic import retain_short_source_audio
from autoclip.narrative import require_commentary
from autoclip.util import read_json
from test_duration_balance import measured
from test_edit_before_balance import setup_pipeline, picker, grow
from test_editorial_preflight import whole_report


@pytest.mark.parametrize('can_balance',[True,False])
def test_source_fallback_below_quantity_and_duration_floors_requires_all_final_gates(tmp_path,monkeypatch,can_balance):
    series,plan,lines,cfg,events=setup_pipeline(monkeypatch,tmp_path)
    before=plan.model_dump()
    revised=retain_short_source_audio(plan,'beat-2',lines,cfg,1360)
    assert revised is not None and revised.clips[0].beats[2].mode=='original'
    assert revised.clips[0].beats[2].text==''
    assert revised.clips[0].beats[2].evidence_ids==[4,5]
    assert plan.model_dump()==before
    with pytest.raises(ValueError,match='碎片过场'):
        require_commentary(revised.clips[0],cfg,measured(revised,cfg))
    assert [i['code'] for i in measured(revised,cfg)['issues']]==['duration_short']
    def preflight(clip,*args,**kwargs):events.append('whole');return whole_report(clip)
    def repair(current,issues,*args,**kwargs):
        events.append('balance')
        assert all(i.get('stage')=='commentary_balance' for i in issues)
        assert all(i['id']!='beat-2' for i in issues)  # The source interval is immutable.
        return grow(current,issues)
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'repair_plan',repair)
    cfg.repair_attempts=2 if can_balance else 0
    if not can_balance:
        with pytest.raises(ValueError,match='自动修复达到上限'):
            series.complete_long_plan(tmp_path/'source.mp4',revised,lines,{},tmp_path,cfg,picker(events,(0,1)))
        assert 'audit' not in events and 'balance' not in events
        assert not (tmp_path/'plan-approved.json').exists()
        assert read_json(tmp_path/'repair-budget.json')['pending']=='commentary'
    else:
        final,report=series.complete_long_plan(tmp_path/'source.mp4',revised,lines,{},tmp_path,cfg,picker(events,(0,1)))
        assert report['passed'] and measured(final,cfg)['issues']==[]
        assert require_commentary(final.clips[0],cfg,measured(final,cfg))['narration_chars']>=1800
        assert final.clips[0].beats[2]==revised.clips[0].beats[2]
        assert events==['measure','whole','choose','balance','measure','whole','audit']
        assert read_json(tmp_path/'plan-approved.json')==final.model_dump()
