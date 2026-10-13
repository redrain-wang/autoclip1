import pytest
from pathlib import Path
from autoclip.config import Settings
from autoclip.models import Line
from autoclip.conflict import prepare_conflict_brief, brief_context
from autoclip.util import read_json


def test_conflict_brief_is_a_source_bound_editorial_suggestion_without_forced_twists(tmp_path):
    video=tmp_path/'06.mp4';video.write_bytes(b'source')
    lines=[Line(id=1,start=1,end=3,text='为什么不还钱'),Line(id=2,start=10,end=12,text='答应明天还')]
    calls=[]
    class Model:
        def json(self,system,prompt):
            calls.append(prompt)
            return {'question':'为什么承诺还没变成还款','why_it_matters':'承诺与兑现的距离',
                    'steps':[{'role':'起因','description':'追问欠款','evidence_ids':[1],'commentary_job':'说明压力'},
                             {'role':'结果','description':'答应明天还','evidence_ids':[2],'commentary_job':'说明只是承诺'},
                             {'role':'转折','description':'下集还清','evidence_ids':[999],'commentary_job':'不得引入'}],
                    'ending':'本段止于承诺','unresolved':['是否到账'],'opening_evidence_ids':[1],
                    'opening_reason':'直接进入问题','omit':['不插入无关的下一场']}
    first=prepare_conflict_brief(video,tmp_path,lines,0,15,Settings(),Model())
    second=prepare_conflict_brief(video,tmp_path,lines,0,15,Settings(),Model())
    assert first==second and len(calls)==1
    assert [s['role'] for s in first['steps']]==['起因','结果']
    assert first['warnings'] and first['status']=='editorial_suggestion_not_reviewed_facts'
    assert '下集还清' not in str(brief_context(first))
    assert (tmp_path/'完整冲突提纲.md').exists()
    assert '不要求每种都有' in calls[0]
    # Re-check cached suggestions as well: distant quotations cannot become
    # one continuous opening simply by using their outer timestamps.
    cache=next((tmp_path/'conflict-briefs').glob('*.raw.json'))
    raw=read_json(cache);raw['opening_evidence_ids']=[1,999]
    from autoclip.util import write_json
    write_json(cache,raw)
    replay=prepare_conflict_brief(video,tmp_path,lines,0,15,Settings(),Model())
    assert replay['opening_evidence_ids']==[] and replay['opening_reason']==''
    assert len(calls)==1


def test_noncontinuous_opening_is_not_used_as_one_original_clip(tmp_path):
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    lines=[Line(id=i,start=i*10,end=i*10+3,text='对白') for i in range(3)]
    class Model:
        def json(self,*args):
            return {'question':'为何如此','why_it_matters':'选择','steps':[],'ending':'仍待后续',
                    'opening_evidence_ids':[0,2],'opening_reason':'两场拼接'}
    result=prepare_conflict_brief(video,tmp_path,lines,0,30,Settings(),Model())
    assert result['opening_source_range'] is None and result['opening_reason']==''
    assert result['warnings']


def test_production_and_long_configs_enable_briefs_without_changing_old_render_fingerprints():
    base=Path(__file__).parents[1]
    for name in ('configs.production.toml','configs.long.toml','configs.long-yunjian.toml'):
        cfg=Settings.load(base/name)
        assert cfg.conflict_brief and 'conflict_brief' not in cfg.model_dump()
