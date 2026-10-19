"""Boundary regressions: actual spoken text must not bypass the shared policy."""
from pathlib import Path

import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Plan
from autoclip.repair import RepairNeeded
from autoclip.util import digest, read_json, source_identity, write_json


def clean_clip():
    return Clip(title='求差事的经过',angle='机会与代价',hook='为何先翻旧账',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=i*10,end=i*10+10,
             text='' if i%2==0 else '年轻人提出请求，长辈随后作了回应。',evidence_ids=[i],reason='剧情承接')
        for i in range(4)])


@pytest.mark.parametrize('frozen',[False,True])
@pytest.mark.parametrize('polish',[False,True])
def test_clean_plan_cannot_hide_production_language_in_cached_spoken_text(tmp_path,monkeypatch,frozen,polish):
    from autoclip import render, identity
    from autoclip.automatic import FrozenAutomaticScript
    clip=clean_clip();cfg=Settings(identity_gate=False,oral_edit=polish)
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    key=digest({'version':render.VERSION,'source':source_identity(video),'clip':clip.model_dump(),
                'cfg':cfg.model_dump(mode='json')})
    folder=tmp_path/'renders'/f'01-{key}';folder.mkdir(parents=True)
    (folder/'video.mp4').write_bytes(b'previous media')
    timeline=[b.model_dump() for b in clip.beats];timeline[1]['text']='字幕里说，这事始终没弄明白。'
    manifest={'duration':40.,'timeline':timeline}
    write_json(folder/'manifest.json',manifest)
    monkeypatch.setattr(identity,'enforce_identity',lambda *a:None)
    monkeypatch.setattr(render,'duration',lambda _:40.)
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        render.render_clip(video,tmp_path,clip,0,cfg,FrozenAutomaticScript() if frozen else object(),[])
    assert read_json(folder/'manifest.json')==manifest
    assert (folder/'video.mp4').read_bytes()==b'previous media'


def test_voice_length_rewrite_is_checked_before_synthesizing_its_bad_text(tmp_path,monkeypatch):
    from autoclip import render
    spoken=[]
    monkeypatch.setattr(render,'synthesize',lambda text,*a:spoken.append(text) or tmp_path/'voice.mp3')
    monkeypatch.setattr(render,'duration',lambda _:20.)
    class Model:
        def json(self,*a):return {'text':'字幕告诉我们，他答应了。'}
    beat=clean_clip().beats[1]
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        render.fit_narration(beat,tmp_path,Settings(oral_edit=False),Model(),[])
    assert spoken==[beat.text]


@pytest.mark.parametrize('frozen',[False,True])
def test_final_actual_timeline_is_checked_even_if_a_rewrite_bypasses_the_fitter(tmp_path,monkeypatch,frozen):
    from autoclip import render, identity, media_cache
    from autoclip.automatic import FrozenAutomaticScript
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    voice=tmp_path/'voice.mp3';voice.write_bytes(b'voice')
    monkeypatch.setattr(identity,'enforce_identity',lambda *a:None)
    monkeypatch.setattr(render,'fit_narration',lambda *a:('镜头里有人应下差事。',voice,10.))
    monkeypatch.setattr(render,'voice_captions',lambda *a:([],'test'))
    monkeypatch.setattr(media_cache,'cached_shot',lambda *a:(tmp_path/'segment.mp4',10.,True))
    monkeypatch.setattr(render,'run',lambda *a,**k:pytest.fail('bad narration reached final encoding'))
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        render.render_clip(video,tmp_path,clean_clip(),0,Settings(identity_gate=False,oral_edit=False),
                           FrozenAutomaticScript() if frozen else object(),[])
    assert not list(tmp_path.rglob('manifest.json'))


@pytest.mark.parametrize('profile',['long','short'])
def test_delivery_gate_checks_actual_narration_before_media_probe(tmp_path,monkeypatch,profile):
    from autoclip import series
    monkeypatch.setattr(series,'duration',lambda *a:pytest.fail('bad narration reached delivery probe'))
    finished={'video':'unused.mp4','timeline':[{'mode':'narration','text':'从画面中可以看出，他已经答应。'}]}
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        series.check_delivery(finished,Settings(duration_profile=profile,target_seconds=1200 if profile=="long" else 190),tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('state',['已完成','部分完成','等待新增声纹确认'])
def test_export_checks_all_episodes_before_replacing_any_delivery(tmp_path,state):
    from autoclip.deliver import export_finished
    for n in (1,2):
        episode=tmp_path/f'{n:02}-source';episode.mkdir()
        media=episode/'video.mp4';media.write_bytes(b'new media')
        write_json(episode/'status.json',{'stage':state})
        text='事情仍在继续发展。' if n==1 else '根据字幕，年轻人已经答应。'
        write_json(episode/'results.json',[{'title':'故事','angle':'剧情','duration':120.,'video':str(media),
            'timeline':[{'mode':'narration','text':text,'start':0,'end':10}]}])
    target=tmp_path/'成片';target.mkdir()
    (target/'用户文件.mp4').write_bytes(b'user media')
    (target/'清单.json').write_text('[]')
    before={p.name:p.read_bytes() for p in target.iterdir()}
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):export_finished(tmp_path)
    assert {p.name:p.read_bytes() for p in target.iterdir()}==before


def test_manual_local_edit_cannot_save_new_production_language(tmp_path):
    from autoclip.revision import edit_narration
    plan=Plan(episode_summary='',genre='',style='',clips=[clean_clip()])
    source=tmp_path/'plan.json';write_json(source,plan.model_dump());before=source.read_bytes()
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        edit_narration(source,tmp_path/'revised.json','字幕显示，这次问答结束了。',beat=1)
    assert source.read_bytes()==before
    assert not (tmp_path/'revised.json').exists() and not (tmp_path/'revised.change.json').exists()


def test_internal_edit_notes_and_original_dialogue_do_not_become_narration_violations():
    from autoclip.editorial import require_audience_timeline
    require_audience_timeline([
        {'mode':'original','text':'字幕里说，我没看明白。'},
        {'mode':'narration','text':'他让人核对账目。','reason':'字幕里说：核对账目'},
    ])


@pytest.mark.parametrize('enabled',[False,True])
def test_packaging_does_not_bypass_narration_policy_when_disabled(tmp_path,enabled):
    from autoclip.branding import BrandSettings,package_clip
    manifest={'timeline':[{'mode':'narration','text':'字幕交代了这次谈话的结局。'}]}
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        package_clip(tmp_path/'source.mp4',tmp_path,manifest,0,'测试剧',
                     BrandSettings(enabled=enabled,watermark=tmp_path/'missing.png'))
    assert not list(tmp_path.iterdir())
