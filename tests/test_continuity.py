from pathlib import Path
import pytest
from autoclip.config import Settings
from autoclip.continuity import load_continuity, continuity_prompt
from autoclip.models import Line, Beat, Clip, Plan
from autoclip.util import digest, read_json, source_identity, write_json


def prior(tmp_path, number, history, series='雍正王朝', source_dir=None):
    source_dir = source_dir or tmp_path/'sources'
    source_dir.mkdir(exist_ok=True)
    video = source_dir/f'{number:02}.mp4'
    video.write_bytes(f'source {number}'.encode())
    folder = history/f'episode-{number}-{series}'
    lines = [Line(id=1,start=1,end=2,text=f'第{number}集承诺还银').model_dump()]
    write_json(folder/'source.json',source_identity(video))
    write_json(folder/'transcript.json',lines)
    write_json(folder/'research.json',{'source':source_identity(video), 'series_profile':{'series':series},
               'transcript_hash':digest(lines), 'windows':[{'events':[{'start':1,'end':2,'event':'承诺还银，未见兑现',
               'participants':['说话人待核'],'identity_basis':'ASR不能认脸','uncertainties':['尚未到账'],'evidence_ids':[1]}]}]})
    return video, folder


def test_prior_episodes_exclude_current_future_other_show_other_folder_and_changed_source(tmp_path):
    history=tmp_path/'history'
    for n in range(1,8): prior(tmp_path,n,history)
    current=tmp_path/'sources/06.mp4'
    prior(tmp_path,4,history,series='另一部剧',source_dir=tmp_path/'another')
    cfg=Settings(series='雍正王朝',continuity_history=history)
    context=load_continuity(current,cfg,tmp_path/'run')
    assert [e['episode'] for e in context['episodes']]==[3,4,5]
    assert '第6集承诺还银' not in continuity_prompt(context)
    old=context['signature']
    (tmp_path/'sources/05.mp4').write_bytes(b'changed source')
    changed=load_continuity(current,cfg)
    assert [e['episode'] for e in changed['episodes']]==[2,3,4]
    assert old!=changed['signature'] and changed['skipped']


def test_commentary_is_separate_and_failed_draft_never_becomes_memory(tmp_path):
    history=tmp_path/'history'
    video,folder=prior(tmp_path,5,history)
    current,_=prior(tmp_path,6,history)
    finished=folder/'film.mp4'; finished.write_bytes(b'film')
    write_json(folder/'plan.json',{'episode_summary':'未通过的错稿'})
    result={'video':str(finished), 'title':'此前的还款选择', 'duration':100,
            'qa':{'branded_full_decode':'passed'},'timeline':[{'start':1,'end':2,'mode':'narration','text':'此前答应还款'}]}
    write_json(folder/'results.json',[result])
    cfg=Settings(series='雍正王朝',continuity_history=history)
    context=load_continuity(current,cfg)
    assert context['episodes'][0]['delivered_commentary'][0]['status']=='delivered_interpretation_not_fact_evidence'
    assert '未通过的错稿' not in continuity_prompt(context)
    original=context['signature']
    result['timeline'][0]['text']='此前只是承诺，还没有到账'
    write_json(folder/'results.json',[result])
    assert load_continuity(current,cfg)['signature']!=original
    write_json(folder/'quality-holds.json',[str(finished)])
    assert load_continuity(current,cfg)['episodes'][0]['delivered_commentary']==[]


def test_missing_history_is_not_a_gate_and_config_paths_are_absolute(tmp_path):
    cfg_path=tmp_path/'config.toml';cfg_path.write_text('series="雍正王朝"\n')
    cfg=Settings.load(cfg_path)
    assert cfg.continuity_history==tmp_path/'output'
    assert cfg.media_cache==tmp_path/'output/.media-cache'
    assert 'media_cache' not in cfg.model_dump() and 'continuity_history' not in cfg.model_dump()
    assert load_continuity(tmp_path/'01.mp4',cfg)['episodes']==[]
    assert load_continuity(tmp_path/'movie.mp4',cfg)['episodes']==[]
    assert continuity_prompt(load_continuity(tmp_path/'01.mp4',cfg))==''


def test_analyze_completes_late_highlight_and_reports_silent_windows(tmp_path):
    from autoclip.director import analyze
    lines=[Line(id=1,start=1,end=2,text='早段高光'),Line(id=2,start=610,end=620,text='尾段更精彩')]
    calls=[]
    class Model:
        def json(self,system,prompt,images):
            calls.append(prompt)
            return {'summary':'已读','candidates':[{'angle':'尾段' if '尾段更精彩' in prompt else '早段'}]}
    cfg=Settings(vision=False,output=tmp_path/'empty')
    maps=analyze(tmp_path/'06.mp4',tmp_path,lines,650,cfg,Model())
    assert [(m['start'],m['end']) for m in maps]==[(0,300),(300,600),(600,650)]
    assert maps[-1]['candidates'][0]['angle']=='尾段'
    coverage=read_json(tmp_path/'analysis-coverage.json')
    assert coverage['all_windows_completed'] and not coverage['stopped_after_candidate']
    assert coverage['windows'][1]['dialogue_lines']==0 and len(calls)==2


def test_failed_scan_cannot_leave_old_success_coverage(tmp_path):
    from autoclip.director import analyze
    write_json(tmp_path/'analysis-coverage.json',{'all_windows_completed':True})
    class Model:
        def json(self,*args): raise RuntimeError('temporary failure')
    with pytest.raises(RuntimeError):
        analyze(tmp_path/'06.mp4',tmp_path,[Line(id=1,start=1,end=2,text='对白')],3,Settings(vision=False),Model())
    assert read_json(tmp_path/'analysis-coverage.json')['all_windows_completed'] is False


def test_prior_context_stays_out_of_current_episode_fact_extraction(tmp_path,monkeypatch):
    from autoclip.research import prepare_research
    video=tmp_path/'06.mp4';video.write_bytes(b'source')
    lines=[Line(id=1,start=1,end=2,text='本集交代')]
    prior_context={'signature':'old','episodes':[{'episode':5,'events':[],'dialogue':[{'id':9,'start':1,'end':2,'text':'此前承诺'}]}]}
    calls=[]
    class Model:
        def json(self,system,prompt):
            calls.append(prompt);return {'events':[],'omitted_reason':'无完整事件'}
    cfg=Settings()
    prepare_research(video,tmp_path/'run',lines,3,cfg,Model(),prior_context)
    prepare_research(video,tmp_path/'run',lines,3,cfg,Model(),prior_context)
    assert len(calls)==1 and '此前承诺' not in calls[0]
    prior_context['signature']='new'
    prior_context['episodes'][0]['dialogue'][0]['text']='此前只是承诺'
    prepare_research(video,tmp_path/'run',lines,3,cfg,Model(),prior_context)
    assert len(calls)==1  # Previous interpretation cannot change this ASR ledger.


def test_context_is_added_only_after_full_current_episode_scan(tmp_path):
    from autoclip.director import analyze
    previous={'signature':'history-version', 'episodes':[{'episode':3,'events':[],
              'dialogue':[{'id':88,'start':1,'end':2,'text':'第三集接风宴'}]}]}
    class Model:
        def json(self,system,prompt,images):
            assert '第三集接风宴' not in prompt
            assert '本集背书' in prompt
            return {'summary':'本集背书','candidates':[]}
    maps=analyze(tmp_path/'06.mp4',tmp_path,[Line(id=1,start=1,end=2,text='本集背书')],3,
                 Settings(vision=False),Model(),previous)
    assert maps[0]['summary']=='本集背书'
    assert '第三集接风宴' in maps[-1]['prior_episode_context']
    assert maps[-1]['continuity_signature']=='history-version'
